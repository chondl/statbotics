"""Manifest integrity (trailing-year refresh spec §8-§9): new manifest fields
round-trip, every write is a pinned-generation conditional write retried on
412, reads fail closed, and per-year hist epochs follow the max rule."""

import gzip
import json

import pytest
from google.api_core.exceptions import ServiceUnavailable

import src.google.parquet as parquet
import src.google.storage as storage
from src.constants import HIST_EPOCH
from src.google.publish import MANIFEST_OBJECT, Manifest, effective_hist_epoch
from tests.fake_gcs import FakeBucket
from tests.test_hist_blobs import make_objs


def seed(bucket: FakeBucket, manifest: Manifest) -> int:
    return bucket.put(
        MANIFEST_OBJECT,
        gzip.compress(manifest.to_json().encode("utf-8")),
        cache_control=storage.MANIFEST_CACHE,
        content_encoding="gzip",
    )


def stored(bucket: FakeBucket) -> Manifest:
    return Manifest.from_json(gzip.decompress(bucket.objects[MANIFEST_OBJECT]))


@pytest.fixture
def bucket(monkeypatch):
    b = FakeBucket()
    monkeypatch.setattr(storage, "_bucket", lambda: b)
    monkeypatch.setattr(parquet, "_bucket", lambda: b)
    return b


# ------------------------------------------------------------- the model


def test_manifest_round_trips_new_and_unknown_fields():
    m = Manifest(
        cycle="c",
        hist_epoch=2,
        blobs={"teams/all": "v2/teams/all.abc"},
        trailing_year=2026,
        hist_epochs={"2026": 3},
        extra={"future_field": {"x": 1}},
    )
    raw = m.to_json()
    data = json.loads(raw)
    assert data["trailing_year"] == 2026
    assert data["hist_epochs"] == {"2026": 3}
    assert data["future_field"] == {"x": 1}
    back = Manifest.from_json(raw)
    assert back == m
    # Defaults: absent fields parse to None / {} / {}.
    old = Manifest.from_json('{"schema": 1, "cycle": "", "hist_epoch": 2, "blobs": {}}')
    assert old.trailing_year is None and old.hist_epochs == {} and old.extra == {}


def test_effective_epoch_max_rule():
    m = Manifest(hist_epoch=2, hist_epochs={"2026": 3, "2024": 1})
    assert effective_hist_epoch(m, 2026) == 3
    assert effective_hist_epoch(m, 2025) == 2  # absent: global epoch
    assert effective_hist_epoch(m, 2024) == 2  # global bump supersedes
    assert effective_hist_epoch(m, None) == 2  # no-year path


# ------------------------------------------------------- update_manifest


def test_update_manifest_preserves_trailing_state(bucket):
    seed(
        bucket,
        Manifest(
            hist_epoch=HIST_EPOCH,
            blobs={"a": "v2/a.1"},
            trailing_year=2026,
            hist_epochs={"2025": HIST_EPOCH + 1},
            extra={"future_field": 7},
        ),
    )
    out = storage.update_manifest(lambda m: m.blobs.update({"b": "v2/b.2"}))
    got = stored(bucket)
    assert got == out
    assert got.blobs == {"a": "v2/a.1", "b": "v2/b.2"}
    assert got.trailing_year == 2026
    assert got.hist_epochs == {"2025": HIST_EPOCH + 1}
    assert got.extra == {"future_field": 7}
    assert got.cycle  # stamped
    meta = bucket.metadata[MANIFEST_OBJECT]
    assert meta["content_encoding"] == "gzip"
    assert meta["cache_control"] == storage.MANIFEST_CACHE


def test_update_manifest_creates_when_absent(bucket):
    storage.update_manifest(lambda m: m.blobs.update({"a": "v2/a.1"}))
    got = stored(bucket)
    assert got.blobs == {"a": "v2/a.1"}
    assert got.hist_epoch == HIST_EPOCH


def test_update_manifest_retries_on_412(bucket):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, blobs={"a": "v2/a.1"}))
    fired = []

    def concurrent_writer(blob):
        if blob.name == MANIFEST_OBJECT and not fired:
            fired.append(1)
            m = stored(bucket)
            m.blobs["theirs"] = "v2/theirs.9"
            seed(bucket, m)

    bucket.before_upload.append(concurrent_writer)
    calls = []

    def mutate(m):
        calls.append(1)
        m.blobs["ours"] = "v2/ours.1"

    storage.update_manifest(mutate)
    got = stored(bucket)
    assert got.blobs["theirs"] == "v2/theirs.9"
    assert got.blobs["ours"] == "v2/ours.1"
    assert got.blobs["a"] == "v2/a.1"
    assert len(calls) == 2  # re-applied to the fresh manifest


def test_update_manifest_reads_pinned_generation(bucket):
    gen_a = seed(bucket, Manifest(hist_epoch=HIST_EPOCH, blobs={"a": "v2/a.1"}))
    fired = []

    def change_between_get_and_download(blob):
        if blob.name == MANIFEST_OBJECT and not fired:
            fired.append(1)
            m = stored(bucket)
            m.blobs["theirs"] = "v2/theirs.9"
            seed(bucket, m)

    bucket.before_download.append(change_between_get_and_download)
    seen = []

    def mutate(m):
        seen.append(dict(m.blobs))
        m.blobs["ours"] = "v2/ours.1"

    storage.update_manifest(mutate)
    # First attempt read the generation get_blob returned, not the newer one.
    assert bucket.downloads[0] == (MANIFEST_OBJECT, gen_a)
    assert seen[0] == {"a": "v2/a.1"}
    # Its write hit 412; the retry saw the concurrent write and won.
    assert seen[1] == {"a": "v2/a.1", "theirs": "v2/theirs.9"}
    got = stored(bucket)
    assert got.blobs == {"a": "v2/a.1", "theirs": "v2/theirs.9", "ours": "v2/ours.1"}


def test_update_manifest_exhaustion_raises_and_counts(bucket, monkeypatch):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    monkeypatch.setattr(storage, "manifest_conflicts", 0)

    def always_conflict(blob):
        if blob.name == MANIFEST_OBJECT:
            m = stored(bucket)
            seed(bucket, m)

    bucket.before_upload.append(always_conflict)
    calls = []
    with pytest.raises(storage.ManifestConflictError):
        storage.update_manifest(lambda m: calls.append(1))
    assert len(calls) == 1 + storage.MANIFEST_RETRIES
    assert storage.manifest_conflicts == 1


def test_read_error_aborts_write(bucket):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, blobs={"a": "v2/a.1"}))

    def boom(blob):
        raise ServiceUnavailable("transient")

    bucket.before_download.append(boom)
    with pytest.raises(storage.ManifestReadError):
        storage.read_manifest()
    calls = []
    with pytest.raises(storage.ManifestReadError):
        storage.update_manifest(lambda m: calls.append(1))
    assert calls == []
    assert MANIFEST_OBJECT not in bucket.uploads


def test_read_manifest_not_found_is_none(bucket):
    assert storage.read_manifest() is None


def test_read_manifest_corrupt_raises(bucket):
    bucket.put(MANIFEST_OBJECT, b"not json")
    with pytest.raises(storage.ManifestReadError):
        storage.read_manifest()


def test_epochs_at_or_below_global_dropped(bucket, monkeypatch):
    seed(
        bucket,
        Manifest(
            hist_epoch=HIST_EPOCH,
            hist_epochs={"2024": HIST_EPOCH, "2025": HIST_EPOCH + 1},
        ),
    )
    storage.update_manifest(lambda m: None)
    assert stored(bucket).hist_epochs == {"2025": HIST_EPOCH + 1}

    # A global bump past every year epoch supersedes and drops them all.
    monkeypatch.setattr(storage, "HIST_EPOCH", HIST_EPOCH + 1)
    storage.update_manifest(lambda m: None)
    got = stored(bucket)
    assert got.hist_epoch == HIST_EPOCH + 1
    assert got.hist_epochs == {}


def test_global_epoch_never_regresses(bucket):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH + 5))
    storage.update_manifest(lambda m: None)
    assert stored(bucket).hist_epoch == HIST_EPOCH + 5


# ------------------------------------------------------------- the writers


def test_gc_refuses_without_manifest(bucket):
    bucket.put("v2/old.abc", b"x")
    assert storage.gc_versioned_blobs(grace_hours=0)["status"] == "refused"
    assert "v2/old.abc" in bucket.objects


def test_gc_refuses_on_read_error(bucket):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    bucket.put("v2/old.abc", b"x")

    def boom(blob):
        raise ServiceUnavailable("transient")

    bucket.before_download.append(boom)
    assert storage.gc_versioned_blobs(grace_hours=0)["status"] == "refused"
    assert "v2/old.abc" in bucket.objects


def test_write_parquet_creates_missing_manifest(bucket):
    objs, teams = make_objs()
    parquet.write_parquet(2025, objs, teams)
    got = stored(bucket)
    logical = parquet.parquet_logical(2025, "team_years")
    assert logical in got.blobs
    assert got.blobs[logical] in bucket.objects
    assert got.hist_epoch == HIST_EPOCH


def test_write_parquet_aborts_on_read_error(bucket):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))

    def boom(blob):
        raise ServiceUnavailable("transient")

    bucket.before_download.append(boom)
    objs, teams = make_objs()
    with pytest.raises(storage.ManifestReadError):
        parquet.write_parquet(2025, objs, teams)
    assert bucket.uploads == []


def test_publish_applies_updates_and_removals(bucket):
    from src.google.publish import UploadPlan

    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, blobs={"a": "v2/a.1", "gone": "v2/g.1"}))
    plan = UploadPlan(
        uploads={"v2/b.2": b"bb"},
        legacy_uploads={"b": b"bb"},
        blob_updates={"b": "v2/b.2"},
        blob_removals={"gone"},
    )
    storage._publish(plan)
    assert stored(bucket).blobs == {"a": "v2/a.1", "b": "v2/b.2"}
    assert bucket.objects["v2/b.2"] == b"bb" and bucket.objects["b"] == b"bb"
    # Manifest written last.
    assert bucket.uploads[-1] == MANIFEST_OBJECT
