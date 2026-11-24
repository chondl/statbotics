"""Per-year hist epochs for past years (trailing-year refresh spec §8):
publish_past_year compares a year's rendered site blobs with what its
effective epoch serves and either does nothing, fills gaps under that epoch,
or advances the whole year to a fresh epoch, so a past-year reprocess can
never leave a page stale."""

import gzip
from typing import Dict

import pytest

import src.constants as constants
import src.google.storage as storage
from src.constants import HIST_EPOCH
from src.data import trailing
from src.google import year_epoch
from src.google.publish import (
    MANIFEST_OBJECT,
    Manifest,
    effective_hist_epoch,
    historical_key,
)
from tests.fake_gcs import FakeBucket

YEAR = 2020


def seed(bucket: FakeBucket, manifest: Manifest) -> int:
    return bucket.put(
        MANIFEST_OBJECT,
        gzip.compress(manifest.to_json().encode("utf-8")),
        cache_control=storage.MANIFEST_CACHE,
        content_encoding="gzip",
    )


def stored(bucket: FakeBucket) -> Manifest:
    return Manifest.from_json(gzip.decompress(bucket.objects[MANIFEST_OBJECT]))


def rendered() -> Dict[str, bytes]:
    return {
        f"team_years/{YEAR}": b"ty",
        f"events/{YEAR}": b"evs",
        f"event/{YEAR}casj": b"casj",
        f"team/254/{YEAR}": b"254",
    }


def seed_hist(bucket: FakeBucket, epoch: int, blobs: Dict[str, bytes]) -> None:
    for path, data in blobs.items():
        bucket.put(historical_key(epoch, path), data)


@pytest.fixture
def bucket(monkeypatch):
    b = FakeBucket()
    monkeypatch.setattr(storage, "_bucket", lambda: b)
    return b


def test_unchanged_year_writes_nothing(bucket, capsys):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    seed_hist(bucket, HIST_EPOCH, rendered())
    bucket.uploads.clear()

    assert year_epoch.publish_past_year(YEAR, rendered()) == "unchanged"

    assert bucket.uploads == []
    assert f"YEAR EPOCH {YEAR} unchanged epoch={HIST_EPOCH}" in capsys.readouterr().out


def test_changed_path_advances_whole_year(bucket, capsys):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    seed_hist(bucket, HIST_EPOCH, rendered())
    bucket.uploads.clear()
    new = rendered()
    new[f"event/{YEAR}casj"] = b"casj-with-offseason"

    assert year_epoch.publish_past_year(YEAR, new) == "advanced"

    e1 = HIST_EPOCH + 1
    # Every path, changed or not, is written under the new epoch...
    for path, data in new.items():
        assert bucket.objects[historical_key(e1, path)] == data
        assert bucket.metadata[historical_key(e1, path)]["cache_control"] == (
            storage.IMMUTABLE_CACHE
        )
    # ...the old epoch is untouched...
    assert bucket.objects[historical_key(HIST_EPOCH, f"event/{YEAR}casj")] == b"casj"
    # ...and the manifest switches the year only after all uploads landed.
    assert bucket.uploads[-1] == MANIFEST_OBJECT
    assert set(bucket.uploads[:-1]) == {historical_key(e1, p) for p in new}
    assert stored(bucket).hist_epochs == {str(YEAR): e1}
    assert f"YEAR EPOCH {YEAR} advanced epoch={e1}" in capsys.readouterr().out


def test_missing_path_advances_whole_year(bucket):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    partial = rendered()
    partial.pop(f"team/254/{YEAR}")
    seed_hist(bucket, HIST_EPOCH, partial)

    assert year_epoch.publish_past_year(YEAR, rendered()) == "advanced"
    assert stored(bucket).hist_epochs == {str(YEAR): HIST_EPOCH + 1}


def test_advance_starts_from_year_epoch(bucket):
    e = HIST_EPOCH + 3
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, hist_epochs={str(YEAR): e}))
    seed_hist(bucket, e, rendered())
    new = rendered()
    new[f"events/{YEAR}"] = b"changed"

    assert year_epoch.publish_past_year(YEAR, new) == "advanced"
    assert bucket.objects[historical_key(e + 1, f"events/{YEAR}")] == b"changed"
    assert stored(bucket).hist_epochs == {str(YEAR): e + 1}


def test_cold_year_gap_fills_under_global_epoch(bucket, capsys):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, blobs={"teams/all": "v2/t.1"}))
    before = bucket.objects[MANIFEST_OBJECT]

    assert year_epoch.publish_past_year(YEAR, rendered()) == "gap_filled"

    for path, data in rendered().items():
        assert bucket.objects[historical_key(HIST_EPOCH, path)] == data
        assert bucket.metadata[historical_key(HIST_EPOCH, path)]["cache_control"] == (
            storage.IMMUTABLE_CACHE
        )
    # Nothing to change in the manifest: it is not rewritten.
    assert bucket.objects[MANIFEST_OBJECT] is before
    assert MANIFEST_OBJECT not in bucket.uploads
    assert f"YEAR EPOCH {YEAR} gap_filled epoch={HIST_EPOCH}" in capsys.readouterr().out


def test_cold_year_without_manifest_gap_fills(bucket):
    assert year_epoch.publish_past_year(YEAR, rendered()) == "gap_filled"
    assert MANIFEST_OBJECT not in bucket.objects
    # Manifest() defaults hist_epoch=1, but readers resolve the code's
    # HIST_EPOCH once any manifest write stamps it: fill there.
    for path, data in rendered().items():
        assert bucket.objects[historical_key(HIST_EPOCH, path)] == data


def test_global_bump_reexports_under_new_epoch(bucket, monkeypatch):
    bumped = HIST_EPOCH + 1
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    seed_hist(bucket, HIST_EPOCH, rendered())
    monkeypatch.setattr(year_epoch, "HIST_EPOCH", bumped)
    monkeypatch.setattr(storage, "HIST_EPOCH", bumped)

    # Unchanged data, but nothing exists under the bumped epoch yet.
    assert year_epoch.publish_past_year(YEAR, rendered()) == "gap_filled"

    # The next manifest write stamps hist_epoch=bumped; pages still resolve.
    storage.update_manifest(lambda m: None, bucket)
    m = stored(bucket)
    assert m.hist_epoch == bumped
    e = effective_hist_epoch(m, YEAR)
    assert e == bumped
    for path, data in rendered().items():
        assert bucket.objects[historical_key(e, path)] == data


def test_removes_shadowing_year_keys_but_keeps_parquet(bucket):
    blobs = {
        f"event/{YEAR}casj": "v2/event/casj.1",
        f"events/{YEAR}": "v2/events.1",
        f"team_years/{YEAR}.limit=100.metric=epa": "v2/ty.1",
        f"noteworthy_matches/{YEAR}": "v2/nm.1",
        f"parquet/{YEAR}/teams": "v2/parquet/teams.1",
        f"event/{YEAR + 1}casj": "v2/other-year.1",
        "team/254": "v2/team254.1",
        "teams/all": "v2/teams.1",
    }
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH, blobs=dict(blobs)))
    seed_hist(bucket, HIST_EPOCH, rendered())

    # Unchanged year: the shadowing entries still go, in one manifest write.
    assert year_epoch.publish_past_year(YEAR, rendered()) == "unchanged"

    assert bucket.uploads.count(MANIFEST_OBJECT) == 1
    assert stored(bucket).blobs == {
        f"parquet/{YEAR}/teams": "v2/parquet/teams.1",
        f"event/{YEAR + 1}casj": "v2/other-year.1",
        "team/254": "v2/team254.1",
        "teams/all": "v2/teams.1",
    }
    assert stored(bucket).hist_epochs == {}


def test_advance_removes_shadowing_keys_in_same_write(bucket):
    seed(
        bucket,
        Manifest(hist_epoch=HIST_EPOCH, blobs={f"events/{YEAR}": "v2/events.1"}),
    )
    seed_hist(bucket, HIST_EPOCH, rendered())
    new = rendered()
    new[f"events/{YEAR}"] = b"changed"

    assert year_epoch.publish_past_year(YEAR, new) == "advanced"

    assert bucket.uploads.count(MANIFEST_OBJECT) == 1
    m = stored(bucket)
    assert m.blobs == {} and m.hist_epochs == {str(YEAR): HIST_EPOCH + 1}


def test_advance_rerunnable_after_crash(bucket):
    seed(bucket, Manifest(hist_epoch=HIST_EPOCH))
    seed_hist(bucket, HIST_EPOCH, rendered())
    new = rendered()
    new[f"event/{YEAR}casj"] = b"changed"

    def crash_on_manifest(blob):
        if blob.name == MANIFEST_OBJECT:
            raise RuntimeError("crash before manifest write")

    # First run: a partial e+1 upload set lands, then the process dies
    # before the manifest switches the year.
    uploaded = []

    def crash_mid_upload(blob):
        if blob.name != MANIFEST_OBJECT:
            uploaded.append(blob.name)
            if len(uploaded) > 2:
                raise RuntimeError("crash mid-upload")

    bucket.before_upload.append(crash_mid_upload)
    with pytest.raises(RuntimeError):
        year_epoch.publish_past_year(YEAR, new)
    bucket.before_upload.clear()
    assert stored(bucket).hist_epochs == {}

    # Second run: all uploads land, the manifest write crashes.
    bucket.before_upload.append(crash_on_manifest)
    with pytest.raises(RuntimeError):
        year_epoch.publish_past_year(YEAR, new)
    bucket.before_upload.clear()
    assert stored(bucket).hist_epochs == {}

    # Readers still resolve the old, complete epoch; a rerun completes.
    assert year_epoch.publish_past_year(YEAR, new) == "advanced"
    e1 = HIST_EPOCH + 1
    for path, data in new.items():
        assert bucket.objects[historical_key(e1, path)] == data
    assert stored(bucket).hist_epochs == {str(YEAR): e1}

    # And a further rerun is a no-op.
    assert year_epoch.publish_past_year(YEAR, new) == "unchanged"


def test_refuses_live_year(bucket):
    with pytest.raises(ValueError, match="live year"):
        year_epoch.publish_past_year(constants.CURR_YEAR, rendered())
    assert bucket.uploads == []


def test_refuses_trailing_year_while_window_open(bucket, trailing_open):
    with pytest.raises(ValueError, match="live year"):
        year_epoch.publish_past_year(trailing.trailing_year(), rendered())
    assert bucket.uploads == []


def test_trailing_year_after_close_is_a_past_year(bucket):
    # The autouse fixture pins the window closed.
    assert year_epoch.publish_past_year(trailing.trailing_year(), rendered()) == (
        "gap_filled"
    )


def test_manifest_read_error_aborts_before_uploads(bucket):
    bucket.put(MANIFEST_OBJECT, b"not json")
    with pytest.raises(storage.ManifestReadError):
        year_epoch.publish_past_year(YEAR, rendered())
    assert bucket.uploads == []
