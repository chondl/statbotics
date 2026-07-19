"""Unit tests for the pure publish logic (no GCS/DB required).

Run from backend/:  python -m pytest tests/ -q
"""

from src.google.publish import (
    HASH_LEN,
    Manifest,
    content_hash,
    historical_key,
    plan_uploads,
    versioned_key,
)


def test_content_hash_is_deterministic_and_length_bounded():
    a = content_hash(b"hello world")
    b = content_hash(b"hello world")
    assert a == b
    assert len(a) == HASH_LEN
    assert content_hash(b"hello world") != content_hash(b"hello worlds")


def test_versioned_key_format():
    assert versioned_key("event/2026casf", "abc123") == "v2/event/2026casf.abc123"
    # logical paths that themselves contain dots still round-trip: hash is last segment
    key = versioned_key("team_years/2026.limit=100.metric=epa", "deadbeef0000")
    assert key.endswith(".deadbeef0000")


def test_historical_key_format():
    assert historical_key(3, "team/254/2018") == "hist/3/team/254/2018"


def test_manifest_round_trip():
    m = Manifest(cycle="c1", hist_epoch=2, blobs={"event/x": "v2/event/x.aaaa"})
    parsed = Manifest.from_json(m.to_json())
    assert parsed == m
    # tolerates bytes and missing optional fields
    assert Manifest.from_json(b'{"blobs": {}}').hist_epoch == 1


def test_manifest_hash_for_extracts_trailing_hash():
    m = Manifest(blobs={"a": "v2/a.1234abcd", "b/c.d": "v2/b/c.d.beefcafe0000"})
    assert m.hash_for("a") == "1234abcd"
    assert m.hash_for("b/c.d") == "beefcafe0000"
    assert m.hash_for("missing") is None


def test_plan_uploads_first_cycle_uploads_everything():
    rendered = {"teams/all": b"A", "event/x": b"B"}
    plan = plan_uploads(rendered, prev=None, cycle="c1")
    assert set(plan.legacy_uploads) == {"teams/all", "event/x"}
    assert len(plan.uploads) == 2
    assert set(plan.manifest.blobs) == {"teams/all", "event/x"}


def test_plan_uploads_copy_on_write_skips_unchanged():
    first = plan_uploads({"a": b"A", "b": b"B"}, prev=None, cycle="c1")
    # b changes, a unchanged
    second = plan_uploads({"a": b"A", "b": b"B2"}, prev=first.manifest, cycle="c2")
    assert set(second.legacy_uploads) == {"b"}
    assert list(second.uploads.values()) == [b"B2"]
    # unchanged 'a' keeps its versioned key across cycles
    assert second.manifest.blobs["a"] == first.manifest.blobs["a"]
    assert second.manifest.blobs["b"] != first.manifest.blobs["b"]


def test_plan_uploads_carries_forward_unrendered_entries():
    # cycle 1 backfilled a historical listing; cycle 2 only renders current-year
    first = plan_uploads({"team_years/2018": b"HIST"}, prev=None, cycle="c1")
    second = plan_uploads({"teams/all": b"NOW"}, prev=first.manifest, cycle="c2")
    assert "team_years/2018" in second.manifest.blobs
    assert second.manifest.blobs["team_years/2018"] == first.manifest.blobs["team_years/2018"]
    assert "teams/all" in second.manifest.blobs


def test_plan_uploads_preserves_and_bumps_hist_epoch():
    first = plan_uploads({"a": b"A"}, prev=Manifest(hist_epoch=5), cycle="c1")
    assert first.manifest.hist_epoch == 5
    bumped = plan_uploads({"a": b"A"}, prev=first.manifest, cycle="c2", hist_epoch=6)
    assert bumped.manifest.hist_epoch == 6
