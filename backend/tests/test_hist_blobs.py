"""DB retirement Phase 3 (design 2026-07-20 §2): db-less historical tooling.

- write_hist_blobs renders one historical year's hist/{epoch}/ site blobs
  from in-memory pipeline objects, byte-identical to what the retired
  backfill_blobs.py script rendered from DB rows for the same objects:
  team_years/{year}, events/{year}, event/{key}, team/{num}/{year}.
- upload semantics unchanged: hist blobs are immutable within a HIST_EPOCH
  (existing objects skipped), and team_years without a team row are skipped.
- process_year's historical branch emits Parquet AND hist blobs db-less.
- process_year seeds prior-year team_years through the backend read
  (DuckDB-over-Parquet db-less) when all_team_years is None.
- reprocess_year needs no DB: process_year + chained full current-year
  re-render (update_curr_year(partial=False) + refresh_teams).
"""

import zlib
from collections import defaultdict
from typing import Any, Dict, List

import orjson

import src.data.main as data_main
import src.google.storage as storage
from src.constants import HIST_EPOCH
from src.data.utils import create_objs
from src.db.models import Event, Match, Team, TeamEvent, TeamYear, Year
from src.google.publish import historical_key
from src.site.event import _read_event, _read_events
from src.site.team import _read_team_year
from src.site.team_year import _read_team_years
from src.types.enums import CompLevel, EventStatus, MatchStatus, MatchWinner

YEAR = 2025


class FakeBlob:
    def __init__(self, bucket: "FakeBucket", name: str):
        self.bucket = bucket
        self.name = name
        self.cache_control = None

    def exists(self) -> bool:
        return self.name in self.bucket.objects

    def upload_from_string(self, data: bytes, content_type: str = "") -> None:
        self.bucket.objects[self.name] = data


class FakeBucket:
    def __init__(self):
        self.objects: Dict[str, bytes] = {}

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self, name)


def make_match(key, event, red, blue, time):
    return Match(
        key=key,
        year=YEAR,
        event=event,
        comp_level=CompLevel.QUAL,
        set_number=1,
        match_number=1,
        status=MatchStatus.COMPLETED,
        winner=MatchWinner.RED,
        red_1=red[0],
        red_2=red[1],
        blue_1=blue[0],
        blue_2=blue[1],
        red_dq="",
        red_surrogate="",
        blue_dq="",
        blue_surrogate="",
        red_score=100,
        blue_score=90,
        time=time,
    )


def make_objs():
    year_obj = Year(year=YEAR)
    team_years = {
        f"254_{YEAR}": TeamYear(team=254, year=YEAR, count=10),
        f"1678_{YEAR}": TeamYear(team=1678, year=YEAR, count=8),
        f"999_{YEAR}": TeamYear(team=999, year=YEAR, count=2),  # no team row
    }
    events = {
        f"{YEAR}casj": Event(
            key=f"{YEAR}casj",
            year=YEAR,
            name="Silicon Valley",
            status=EventStatus.COMPLETED,
            week=2,
            time=100,
        ),
        f"{YEAR}cc": Event(
            key=f"{YEAR}cc",
            year=YEAR,
            name="Chezy Champs",
            status=EventStatus.COMPLETED,
            week=9,
            time=200,
        ),
    }
    team_events = {
        f"254_{YEAR}casj": TeamEvent(team=254, year=YEAR, event=f"{YEAR}casj"),
        f"1678_{YEAR}casj": TeamEvent(team=1678, year=YEAR, event=f"{YEAR}casj"),
        f"254_{YEAR}cc": TeamEvent(team=254, year=YEAR, event=f"{YEAR}cc"),
    }
    matches = {
        f"{YEAR}casj_qm1": make_match(
            f"{YEAR}casj_qm1", f"{YEAR}casj", (254, 1678), (118, 148), 110
        ),
        f"{YEAR}cc_qm1": make_match(
            f"{YEAR}cc_qm1", f"{YEAR}cc", (254, 118), (1678, 148), 210
        ),
    }
    objs = (year_obj, team_years, events, team_events, matches, {})
    teams = [
        Team(team=254, name="The Cheesy Poofs", rookie_year=1999),
        Team(team=1678, name="Citrus Circuits", rookie_year=2004),
    ]
    return objs, teams


def compress(data: Any) -> bytes:
    return zlib.compress(orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS))


def expected_backfill_render(objs, teams) -> Dict[str, bytes]:
    """What backfill_blobs.backfill_year rendered + where it wrote it, for
    these objects: the parity oracle for write_hist_blobs."""
    year_obj = objs[0]
    team_years = list(objs[1].values())
    events = list(objs[2].values())
    team_events = list(objs[3].values())
    matches = list(objs[4].values())
    teams_by_num = {t.team: t for t in teams}

    matches_by_event: Dict[str, List] = defaultdict(list)
    team_events_by_event: Dict[str, List] = defaultdict(list)
    for m in matches:
        matches_by_event[m.event].append(m)
    for te in team_events:
        team_events_by_event[te.event].append(te)
    matches_by_team: Dict[int, List] = defaultdict(list)
    for m in matches:
        for num in set(m.get_red()) | set(m.get_blue()):
            matches_by_team[num].append(m)
    team_events_by_team: Dict[int, List] = defaultdict(list)
    for te in team_events:
        team_events_by_team[te.team].append(te)

    out: Dict[str, bytes] = {}
    out[historical_key(HIST_EPOCH, f"team_years/{YEAR}")] = compress(
        _read_team_years(YEAR, year_obj, team_years)
    )
    out[historical_key(HIST_EPOCH, f"events/{YEAR}")] = compress(
        _read_events(year_obj, events)
    )
    for event in events:
        out[historical_key(HIST_EPOCH, f"event/{event.key}")] = compress(
            _read_event(
                year_obj,
                event,
                matches_by_event.get(event.key, []),
                team_events_by_event.get(event.key, []),
            )
        )
    for ty in team_years:
        team_obj = teams_by_num.get(ty.team)
        if team_obj is None:
            continue
        out[historical_key(HIST_EPOCH, f"team/{ty.team}/{YEAR}")] = compress(
            _read_team_year(
                year_obj,
                team_obj,
                ty,
                team_events_by_team.get(ty.team, []),
                matches_by_team.get(ty.team, []),
            )
        )
    return out


# ------------------- write_hist_blobs (backfill_blobs parity) -----------------


def test_write_hist_blobs_matches_backfill_blobs_rendering(monkeypatch):
    objs, teams = make_objs()
    bucket = FakeBucket()
    monkeypatch.setattr(storage, "_bucket", lambda: bucket)

    written = storage.write_hist_blobs(YEAR, objs, teams)

    expected = expected_backfill_render(objs, teams)
    assert set(bucket.objects) == set(expected)
    for name, data in expected.items():
        assert bucket.objects[name] == data, f"payload mismatch for {name}"
    # 999 has a TeamYear but no team row: skipped, exactly as backfill_blobs.
    assert historical_key(HIST_EPOCH, f"team/999/{YEAR}") not in bucket.objects
    assert written == len(expected)


def test_write_hist_blobs_skips_existing_objects(monkeypatch):
    objs, teams = make_objs()
    bucket = FakeBucket()
    existing = historical_key(HIST_EPOCH, f"team_years/{YEAR}")
    bucket.objects[existing] = b"already-there"
    monkeypatch.setattr(storage, "_bucket", lambda: bucket)

    written = storage.write_hist_blobs(YEAR, objs, teams)

    # Immutable within an epoch: the existing blob is untouched.
    assert bucket.objects[existing] == b"already-there"
    assert written == len(expected_backfill_render(objs, teams)) - 1


def test_write_hist_blobs_sets_immutable_cache_control(monkeypatch):
    objs, teams = make_objs()
    bucket = FakeBucket()
    seen: List[Any] = []
    real_blob = FakeBucket.blob

    def spy_blob(self, name):
        b = real_blob(self, name)
        seen.append(b)
        return b

    monkeypatch.setattr(FakeBucket, "blob", spy_blob)
    monkeypatch.setattr(storage, "_bucket", lambda: bucket)
    storage.write_hist_blobs(YEAR, objs, teams)
    assert seen and all(b.cache_control == storage.IMMUTABLE_CACHE for b in seen)


# ---------------- process_year historical branch (db-less) --------------------


class NoTbaCache:
    def hydrate(self, year=None):
        pass

    def persist(self):
        pass

    def set_force_refresh(self, on):
        pass


def _stub_stages(monkeypatch, ingested_ty=None):
    def fake_process_year_tba(year_num, teams, objs, tba_partial, cache):
        if ingested_ty is not None:
            objs[1][f"{ingested_ty.team}_{year_num}"] = ingested_ty
        return [], objs

    monkeypatch.setattr(data_main, "tba_cache", NoTbaCache())
    monkeypatch.setattr(data_main, "process_year_tba", fake_process_year_tba)
    monkeypatch.setattr(data_main, "process_year_avg", lambda year_obj, m: year_obj)
    monkeypatch.setattr(data_main, "process_year_wins", lambda objs: objs)
    monkeypatch.setattr(data_main, "process_year_epa", lambda objs, aty: objs)


def test_dbless_historical_process_year_emits_parquet_and_hist_blobs(monkeypatch):
    monkeypatch.setattr(data_main, "DISABLE_GCS", False)
    _stub_stages(monkeypatch, ingested_ty=TeamYear(team=254, year=YEAR, count=3))

    parquet_calls: List[Any] = []
    hist_calls: List[Any] = []
    monkeypatch.setattr(
        data_main,
        "write_parquet",
        lambda year, objs, teams: parquet_calls.append((year, objs, teams)),
    )
    monkeypatch.setattr(
        data_main,
        "write_hist_blobs",
        lambda year, objs, teams: hist_calls.append((year, objs, teams)),
    )
    teams = [Team(team=254, name="The Cheesy Poofs", rookie_year=1999)]
    teams, objs = data_main.process_year(
        YEAR, False, False, True, teams, create_objs(YEAR), {}
    )

    assert len(parquet_calls) == 1 and len(hist_calls) == 1
    assert parquet_calls[0][0] == YEAR and hist_calls[0][0] == YEAR
    # Both emissions see the same post-EPA pipeline objects and teams.
    assert hist_calls[0][1] is parquet_calls[0][1] is objs
    assert hist_calls[0][2] is parquet_calls[0][2] is teams


def test_dbless_prior_year_seeding_reads_backend(monkeypatch):
    """all_team_years=None seeds EPA from prior-year team_years via the
    backend read — DuckDB-over-Parquet (mocked here)."""
    monkeypatch.setattr(data_main, "DISABLE_GCS", True)
    _stub_stages(monkeypatch)

    requested_years: List[int] = []
    prior = TeamYear(team=254, year=YEAR - 1, count=12, norm_epa=1700.0)

    def fake_get_team_years(year=None, **kw):
        requested_years.append(year)
        return [prior] if year == YEAR - 1 else []

    monkeypatch.setattr(data_main, "get_team_years_db", fake_get_team_years)

    seeded: List[Any] = []

    def spy_epa(objs, all_team_years):
        seeded.append(all_team_years)
        return objs

    monkeypatch.setattr(data_main, "process_year_epa", spy_epa)
    data_main.db_less_seed_incomplete = False

    teams = [Team(team=254, name="The Cheesy Poofs", rookie_year=1999)]
    data_main.process_year(YEAR, False, False, True, teams, create_objs(YEAR), None)

    assert requested_years == list(range(YEAR - 4, YEAR))
    assert seeded and seeded[0][YEAR - 1][254] is prior
    assert data_main.db_less_seed_incomplete is False


# ------------------------- reprocess_year (db-less) ---------------------------


def test_reprocess_year_dbless_chains_curr_year_re_render(monkeypatch):
    calls: List[Any] = []

    monkeypatch.setattr(
        data_main, "load_teams_tba", lambda cache=True: [Team(team=254, name="x")]
    )

    def fake_process_year(year_num, partial, tba_partial, cache, teams, objs, aty):
        calls.append(("process_year", year_num, partial, tba_partial, cache, aty))
        return teams, objs

    monkeypatch.setattr(data_main, "process_year", fake_process_year)
    monkeypatch.setattr(
        data_main,
        "update_curr_year",
        lambda partial, tba_partial: calls.append(
            ("update_curr_year", partial, tba_partial)
        ),
    )
    monkeypatch.setattr(
        data_main, "refresh_teams", lambda: calls.append(("refresh_teams",))
    )

    data_main.reprocess_year(YEAR)

    assert calls == [
        ("process_year", YEAR, False, False, True, None),
        ("update_curr_year", False, False),
        ("refresh_teams",),
    ]


def test_reprocess_year_has_no_backfill_blobs_dependency():
    """The DB-reading backfill scripts are retired: reprocess_year must not
    reference them."""
    import inspect

    src = inspect.getsource(data_main.reprocess_year)
    assert "backfill_blobs" not in src
