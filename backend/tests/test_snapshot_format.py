import json
import pickle
import zlib

import attr
import numpy as np
import pytest
import zstandard

import src.google.snapshot as snap
from src.db.models import ETag, Event, Match, Team, TeamEvent, TeamYear, Year
from src.types.enums import (CompLevel, EventStatus, EventType, MatchStatus,
                             MatchWinner)

ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"


def make_snapshot():
    year = Year(year=2026)
    team_years = {
        "254_2026": TeamYear(team=254, year=2026, epa=np.float64(90.25)),
        "1678_2026": TeamYear(team=1678, year=2026, epa=np.float64(88.5)),
    }
    events = {
        "2026cc": Event(
            key="2026cc",
            year=2026,
            name="Chezy Champs",
            type=EventType.OFFSEASON,
            status=EventStatus.COMPLETED,
            week=9,
        )
    }
    team_events = {
        "254_2026cc": TeamEvent(team=254, year=2026, event="2026cc"),
        "1678_2026cc": TeamEvent(team=1678, year=2026, event="2026cc"),
    }
    matches = {
        "2026cc_qm1": Match(
            key="2026cc_qm1",
            year=2026,
            event="2026cc",
            comp_level=CompLevel.QUAL,
            set_number=1,
            match_number=1,
            status=MatchStatus.COMPLETED,
            winner=MatchWinner.RED,
            red_1=254,
            red_2=1678,
            blue_1=118,
            blue_2=148,
            red_score=100,
            blue_score=90,
        )
    }
    etags = {
        "/events/2026": ETag(year=2026, path="/events/2026", etag="abc123"),
    }
    objs = (year, team_years, events, team_events, matches, etags)
    teams = [
        Team(team=254, name="The Cheesy Poofs", active=True),
        Team(team=1678, name="Citrus Circuits", active=True),
    ]
    return objs, teams


def legacy_serialize(objs, teams):
    # Byte-for-byte replica of the pre-schema-2 json+zlib writer.
    def dump_values(d):
        return [attr.asdict(o) for o in sorted(d.values(), key=lambda o: o.pk())]

    payload = {
        "schema": 1,
        "year": objs[0].year,
        "teams": [attr.asdict(t) for t in sorted(teams, key=lambda t: t.team)],
        "objs": {
            "year": attr.asdict(objs[0]),
            "team_years": dump_values(objs[1]),
            "events": dump_values(objs[2]),
            "team_events": dump_values(objs[3]),
            "matches": dump_values(objs[4]),
            "etags": dump_values(objs[5]),
        },
    }
    return zlib.compress(json.dumps(payload).encode("utf-8"))


def assert_snapshot_equal(got, expected):
    # Model.__eq__ compares pk() only, so compare full field dicts instead.
    got_objs, got_teams = got
    exp_objs, exp_teams = expected
    assert attr.asdict(got_objs[0]) == attr.asdict(exp_objs[0])
    for i in range(1, 6):
        assert set(got_objs[i]) == set(exp_objs[i])
        for k in exp_objs[i]:
            assert attr.asdict(got_objs[i][k]) == attr.asdict(exp_objs[i][k])
    key = lambda t: t.team  # noqa: E731
    assert [attr.asdict(t) for t in sorted(got_teams, key=key)] == [
        attr.asdict(t) for t in sorted(exp_teams, key=key)
    ]


def test_round_trip_equality():
    objs, teams = make_snapshot()
    raw = snap.serialize(objs, teams)
    assert raw[:4] == ZSTD_MAGIC
    assert_snapshot_equal(snap.deserialize(raw), (objs, teams))


def test_round_trip_preserves_enum_types():
    objs, teams = make_snapshot()
    got_objs, _ = snap.deserialize(snap.serialize(objs, teams))
    event = got_objs[2]["2026cc"]
    match = got_objs[4]["2026cc_qm1"]
    assert isinstance(event.type, EventType)
    assert isinstance(event.status, EventStatus)
    assert isinstance(match.comp_level, CompLevel)
    assert isinstance(match.status, MatchStatus)
    assert isinstance(match.winner, MatchWinner)


def test_legacy_bytes_read_through():
    objs, teams = make_snapshot()
    raw = legacy_serialize(objs, teams)
    assert raw[:1] == b"\x78"
    got = snap.deserialize(raw)
    assert_snapshot_equal(got, (objs, teams))
    # Legacy path must coerce enum strings back to enum members.
    assert isinstance(got[0][2]["2026cc"].type, EventType)
    assert isinstance(got[0][4]["2026cc_qm1"].winner, MatchWinner)


def test_deserialize_rejects_unknown_magic():
    with pytest.raises(Exception):
        snap.deserialize(b"not a snapshot")


class FakeBlob:
    def __init__(self, raw):
        self.raw = raw

    def download_as_bytes(self):
        return self.raw


class FakeBucket:
    def __init__(self, raw):
        self.raw = raw

    def blob(self, key):
        return FakeBlob(self.raw)


def patch_bucket(monkeypatch, raw):
    monkeypatch.setattr(snap, "_bucket", lambda: FakeBucket(raw))


def new_format_blob(schema=None, fingerprint=None):
    objs, teams = make_snapshot()
    payload = {
        "schema": snap.SNAPSHOT_SCHEMA if schema is None else schema,
        "fingerprint": (
            snap._models_fingerprint() if fingerprint is None else fingerprint
        ),
        "year": 2026,
        "teams": teams,
        "objs": objs,
    }
    return zstandard.ZstdCompressor(level=3).compress(pickle.dumps(payload, protocol=5))


def test_read_snapshot_happy_path(monkeypatch):
    objs, teams = make_snapshot()
    patch_bucket(monkeypatch, snap.serialize(objs, teams))
    got = snap.read_snapshot(2026)
    assert got is not None
    assert_snapshot_equal(got, (objs, teams))


def test_read_snapshot_rejects_corrupt_bytes(monkeypatch):
    patch_bucket(monkeypatch, ZSTD_MAGIC + b"\x00garbage")
    assert snap.read_snapshot(2026) is None


def test_read_snapshot_rejects_unknown_format(monkeypatch):
    patch_bucket(monkeypatch, b"not a snapshot")
    assert snap.read_snapshot(2026) is None


def test_read_snapshot_rejects_wrong_schema(monkeypatch):
    patch_bucket(monkeypatch, new_format_blob(schema=3))
    assert snap.read_snapshot(2026) is None


def test_read_snapshot_rejects_wrong_fingerprint(monkeypatch):
    patch_bucket(monkeypatch, new_format_blob(fingerprint="0" * 16))
    assert snap.read_snapshot(2026) is None
