"""DB retirement Phase 1 (design 2026-07-20 §2): the pipeline owns Team
lifecycle state, GCS the store. The SQL functions named below were deleted in
Phase 4 — these tests pin the db-less replacements to the semantics they had.

- derive_team_districts parity with the retired SQL update_team_districts:
  Team.district = the district of the team's most recent TeamYear — the latest
  year's value even when null, and null when the team has no TeamYears (scalar
  subquery semantics).
- prune_teams parity with the retired remove_teams_with_no_events Team
  deletion predicate: drop teams with zero TeamEvents across all years AND
  rookie_year < CURR_YEAR (NULL rookie_year and current/future rookies
  survive, as in SQL).
- Full-cycle publish ordering: post_process mutates teams BEFORE the
  current-year publish, so published snapshot/Parquet/blobs carry career
  records, norm_epa, active, last_active_year, district.
- refresh_teams persistence republishes exactly the affected artifacts.
"""

import io
from collections import defaultdict
from copy import deepcopy
from typing import Any, Dict, List

import pyarrow.parquet as pq

import src.data.main as data_main
import src.google.storage as storage
from src.constants import CURR_YEAR
from src.data.teams import derive_team_districts, prune_teams
from src.db.models import Team, TeamEvent, TeamYear
from src.google.parquet import parquet_logical
from src.google.publish import Manifest, UploadPlan

YEAR = CURR_YEAR


def team(num, rookie_year=2010, name="Some Team", **kw):
    return Team(team=num, name=name, rookie_year=rookie_year, **kw)


def ty(num, year, district=None, **kw):
    return TeamYear(team=num, year=year, district=district, **kw)


def as_all_ty(tys: List[TeamYear]) -> Dict[int, Dict[int, TeamYear]]:
    out: Dict[int, Dict[int, TeamYear]] = defaultdict(dict)
    for t in tys:
        out[t.year][t.team] = t
    return out


# ------------------- derive_team_districts (SQL parity) -----------------------


def test_district_latest_team_year_wins():
    teams = [team(254)]
    all_ty = as_all_ty([ty(254, 2024, "fim"), ty(254, 2026, "ont")])
    derive_team_districts(teams, all_ty)
    assert teams[0].district == "ont"


def test_district_latest_year_null_overrides_older_value():
    # SQL takes the latest TeamYear row's district even when it is null
    # (ORDER BY year DESC LIMIT 1) — not "latest non-null".
    teams = [team(254, district="fim")]
    all_ty = as_all_ty([ty(254, 2024, "fim"), ty(254, 2026, None)])
    derive_team_districts(teams, all_ty)
    assert teams[0].district is None


def test_district_no_team_years_becomes_null():
    # Scalar subquery over zero rows is NULL: a stale value is cleared.
    teams = [team(254, district="fim")]
    derive_team_districts(teams, as_all_ty([]))
    assert teams[0].district is None


def test_district_other_teams_do_not_leak():
    teams = [team(254), team(1678)]
    all_ty = as_all_ty([ty(254, 2026, "chs"), ty(1678, 2026, "pnw")])
    derive_team_districts(teams, all_ty)
    assert teams[0].district == "chs"
    assert teams[1].district == "pnw"


# ----------------------- prune_teams (SQL parity) -----------------------------


def test_prune_drops_never_played_past_rookie():
    kept = prune_teams([team(999, rookie_year=2010)], event_teams=set())
    assert kept == []


def test_prune_keeps_team_with_any_event():
    teams = [team(254, rookie_year=2010)]
    assert prune_teams(teams, event_teams={254}) == teams


def test_prune_keeps_null_rookie_year():
    # SQL: NULL < CURR_YEAR is unknown, so the row is not deleted.
    teams = [team(999, rookie_year=None)]
    assert prune_teams(teams, event_teams=set()) == teams


def test_prune_keeps_current_and_future_rookies():
    teams = [team(998, rookie_year=CURR_YEAR), team(999, rookie_year=CURR_YEAR + 1)]
    assert prune_teams(teams, event_teams=set()) == teams


def test_prune_preserves_order_and_filters_only_prunable():
    teams = [
        team(148, rookie_year=2003),  # historical events only
        team(999, rookie_year=2010),  # never played
        team(254, rookie_year=2010),
    ]
    assert prune_teams(teams, event_teams={148, 254}) == [teams[0], teams[2]]


# --------------- full-cycle publish ordering (integration) --------------------


class PipelineHarness:
    """Monkeypatched full cycle: process_year's stage functions are stubbed to
    identity, TBA injects a fixed current-year state, and the GCS writes are
    recorded (with deepcopied teams, so we see field values AT publish
    time)."""

    def __init__(self, monkeypatch):
        self.events: List[str] = []
        self.snapshot_calls: List[Any] = []
        self.storage_calls: List[Any] = []

        mp = monkeypatch
        mp.setattr(data_main, "DISABLE_GCS", False)

        class NoCache:
            def hydrate(self, year=None):
                pass

            def persist(self):
                pass

            def set_force_refresh(self, on):
                pass

        mp.setattr(data_main, "tba_cache", NoCache())

        # Teams entering the cycle: 254 (plays this year), 148 (historical
        # events only), 999 (never played any event, past rookie year).
        self.start_teams = [
            team(254, rookie_year=1999, name="The Cheesy Poofs"),
            team(148, rookie_year=2003, name="Robowranglers"),
            team(999, rookie_year=2010, name="Ghost Team"),
        ]
        mp.setattr(
            data_main, "load_teams_tba", lambda cache=True: deepcopy(self.start_teams)
        )

        # Historical team_years (prior year), read from the backend.
        self.hist_tys = [
            ty(
                254,
                YEAR - 1,
                "fim",
                wins=10,
                losses=2,
                ties=0,
                count=12,
                norm_epa=1700.0,
            ),
            ty(148, 2004, None, wins=5, losses=5, ties=0, count=10, norm_epa=1550.0),
        ]
        mp.setattr(data_main, "get_team_years_db", lambda **kw: deepcopy(self.hist_tys))

        # Current-year TBA ingest: one team_year + team_event for 254.
        def fake_process_year_tba(year_num, teams, objs, tba_partial, cache):
            self.events.append("tba")
            objs[1][f"254_{year_num}"] = ty(
                254,
                year_num,
                "fim",
                wins=2,
                losses=1,
                ties=0,
                count=3,
                norm_epa=1800.0,
            )
            objs[3][f"254_{year_num}cc"] = TeamEvent(
                team=254, year=year_num, event=f"{year_num}cc"
            )
            return [], objs

        mp.setattr(data_main, "process_year_tba", fake_process_year_tba)
        mp.setattr(data_main, "process_year_avg", lambda year_obj, matches: year_obj)
        mp.setattr(data_main, "process_year_wins", lambda objs: objs)
        mp.setattr(data_main, "process_year_epa", lambda objs, aty: objs)

        # Stored TeamEvent teams: 148 only — 254 must be kept via the union
        # with the in-memory current-cycle team_events.
        mp.setattr(data_main, "get_team_event_teams_db", lambda: {148})

        def fake_write_snapshot(year, objs, teams):
            self.events.append("snapshot")
            self.snapshot_calls.append((year, deepcopy(teams)))

        def fake_write_objs_storage(
            objs, orig_objs=None, teams=None, parquet=None, orig_teams=None
        ):
            self.events.append("storage")
            self.storage_calls.append(
                {
                    "teams": deepcopy(teams),
                    "orig_objs": orig_objs,
                    "orig_teams": orig_teams,
                    "parquet": parquet,
                }
            )

        mp.setattr(data_main, "write_snapshot", fake_write_snapshot)
        mp.setattr(data_main, "write_objs_storage", fake_write_objs_storage)
        mp.setattr(data_main, "read_snapshot", lambda year: None)

    def run_full_cycle(self):
        data_main.update_curr_year(partial=False, tba_partial=False)

    def run_partial_cycle(self):
        data_main.update_curr_year(partial=True, tba_partial=True)

    def published_teams(self) -> List[Team]:
        assert len(self.storage_calls) == 1
        return self.storage_calls[0]["teams"]


def test_full_cycle_publishes_post_processed_team_fields(monkeypatch):
    h = PipelineHarness(monkeypatch)
    h.run_full_cycle()

    # Exactly one publish, after post_process.
    teams = {t.team: t for t in h.published_teams()}
    t254 = teams[254]
    assert t254.wins == 12 and t254.count == 15  # career record incl. history
    assert t254.norm_epa == 1800.0  # latest-year norm_epa
    assert t254.active is True
    assert t254.last_active_year == YEAR
    assert t254.district == "fim"

    t148 = teams[148]
    assert t148.active is False
    assert t148.last_active_year == 2004
    assert t148.norm_epa == 1550.0

    # Full-cycle publish renders with no baselines (render-all; the team-blob
    # gate only applies on partial cycles).
    assert h.storage_calls[0]["orig_objs"] is None
    assert h.storage_calls[0]["orig_teams"] is None


def test_full_cycle_prunes_never_played_teams_from_publish(monkeypatch):
    h = PipelineHarness(monkeypatch)
    h.run_full_cycle()

    nums = {t.team for t in h.published_teams()}
    assert nums == {254, 148}  # 999 pruned; 148 kept via stored events

    # Snapshot carries the same pruned, post-processed teams (it is the
    # pipeline state partial cycles resume from).
    (snap_year, snap_teams) = h.snapshot_calls[-1]
    assert snap_year == YEAR
    assert {t.team for t in snap_teams} == {254, 148}
    assert {t.team: t for t in snap_teams}[254].norm_epa == 1800.0


def test_full_cycle_parquet_teams_table_carries_post_processed_fields(monkeypatch):
    h = PipelineHarness(monkeypatch)
    h.run_full_cycle()

    parquet = h.storage_calls[0]["parquet"]
    table = pq.read_table(io.BytesIO(parquet[parquet_logical(YEAR, "teams")]))
    rows = {r["team"]: r for r in table.to_pylist()}
    assert set(rows) == {148, 254}
    assert rows[254]["wins"] == 12
    assert rows[254]["norm_epa"] == 1800.0
    assert rows[254]["district"] == "fim"
    assert rows[254]["active"] is True


def test_full_cycle_publishes_blobs_before_snapshot(monkeypatch):
    # The publish comes after every mutation, and within it: blobs/manifest
    # first, snapshot last (gate-baseline invariant — the snapshot may only
    # record already-published state).
    h = PipelineHarness(monkeypatch)
    h.run_full_cycle()

    assert h.events.index("tba") < h.events.index("storage")
    assert h.events.index("storage") < h.events.index("snapshot")


# ----------------- full-cycle failure paths (clobber guards) ------------------


def _raise(*args: Any, **kw: Any) -> Any:
    raise RuntimeError("backend read unavailable")


def test_full_cycle_dbless_team_years_failure_skips_publish(monkeypatch):
    # Invariant: db-less, a full cycle may only publish teams that went
    # through post_process. If the all-years team_years read fails, the teams
    # list is fresh from load_teams_tba (career fields empty) and publishing
    # would clobber good blobs and the snapshot — so nothing is published.
    h = PipelineHarness(monkeypatch)
    monkeypatch.setattr(data_main, "get_team_years_db", _raise)
    monkeypatch.setattr(data_main, "db_less_publish_skipped", False)
    h.run_full_cycle()

    # publish_curr_year never ran: prior published artifacts (snapshot,
    # Parquet, site blobs in the fake GCS recorders) are untouched.
    assert h.snapshot_calls == []
    assert h.storage_calls == []
    # Degradation is loud: surfaced via /info DB_LESS_PUBLISH_SKIPPED.
    assert data_main.db_less_publish_skipped is True


def test_full_cycle_dbless_successful_publish_clears_skip_flag(monkeypatch):
    h = PipelineHarness(monkeypatch)
    monkeypatch.setattr(data_main, "db_less_publish_skipped", True)
    h.run_full_cycle()
    assert len(h.storage_calls) == 1
    assert data_main.db_less_publish_skipped is False


def test_full_cycle_dbless_storage_failure_leaves_snapshot_behind(monkeypatch):
    # Same gate-baseline invariant in publish_curr_year: the snapshot it
    # writes is the baseline the NEXT partial cycle diffs against, so a
    # storage failure must leave it un-advanced.
    import pytest

    h = PipelineHarness(monkeypatch)
    monkeypatch.setattr(data_main, "write_objs_storage", _raise)
    with pytest.raises(RuntimeError):
        h.run_full_cycle()
    assert h.snapshot_calls == []


def test_publish_prune_skipped_when_stored_team_events_unreadable(monkeypatch):
    # If the stored TeamEvent set cannot be read, the no-event prune is
    # skipped for the cycle: every team is kept (999 included), never pruned
    # from an unreadable-as-empty set.
    h = PipelineHarness(monkeypatch)
    monkeypatch.setattr(data_main, "get_team_event_teams_db", _raise)
    h.run_full_cycle()

    assert {t.team for t in h.published_teams()} == {254, 148, 999}


def test_duckdb_team_event_teams_empty_glob_raises(monkeypatch, tmp_path):
    # A missing/empty team_events Parquet glob must raise (feeding the
    # skip-prune path above), never return an authoritative empty set.
    import pytest

    import src.db_duckdb.main as duck

    monkeypatch.setattr(duck, "_sync", lambda: str(tmp_path))
    with pytest.raises(RuntimeError, match="no team_events Parquet"):
        duck.get_team_event_teams()


# ------------ partial-cycle snapshot-miss guard (db-less clobber) -------------


def test_partial_cycle_dbless_snapshot_miss_skips_cycle(monkeypatch):
    # Db-less, a partial cycle whose snapshot read fails must SKIP the cycle
    # entirely: the fallback objs would be EMPTY (create_objs), and the TBA
    # tier logic skips out-of-window completed events with fresh manifest
    # validations — so the cycle would publish near-empty objs over good
    # snapshot/blobs/Parquet. Nothing processed, nothing written.
    h = PipelineHarness(monkeypatch)  # read_snapshot -> None
    monkeypatch.setattr(data_main, "db_less_partial_skipped", False)
    h.run_partial_cycle()

    assert "tba" not in h.events  # no TBA processing at all
    assert h.snapshot_calls == []  # fake GCS untouched
    assert h.storage_calls == []
    # Degradation is loud: surfaced via /info DB_LESS_PARTIAL_SKIPPED.
    assert data_main.db_less_partial_skipped is True


def test_partial_cycle_dbless_snapshot_hit_runs_and_clears_flag(monkeypatch):
    # The next successful snapshot read resumes normal operation and clears
    # the skip flag.
    from src.data.utils import create_objs

    h = PipelineHarness(monkeypatch)
    snap = (create_objs(YEAR), deepcopy(h.start_teams))
    monkeypatch.setattr(data_main, "read_snapshot", lambda year: deepcopy(snap))
    monkeypatch.setattr(data_main, "db_less_partial_skipped", True)
    h.run_partial_cycle()

    assert "tba" in h.events
    assert len(h.snapshot_calls) == 1
    assert len(h.storage_calls) == 1
    assert data_main.db_less_partial_skipped is False
    # Blobs/manifest first, snapshot last (gate-baseline invariant).
    assert h.events.index("storage") < h.events.index("snapshot")


def test_partial_cycle_dbless_storage_failure_leaves_snapshot_behind(monkeypatch):
    # Gate-baseline invariant in process_year's partial publish: if the
    # storage publish throws mid-cycle, the snapshot must NOT have advanced.
    # Snapshot behind -> the next cycle re-diffs and over-renders (safe);
    # snapshot ahead would mask the unpublished changes as "unchanged".
    import pytest

    from src.data.utils import create_objs

    h = PipelineHarness(monkeypatch)
    snap = (create_objs(YEAR), deepcopy(h.start_teams))
    monkeypatch.setattr(data_main, "read_snapshot", lambda year: deepcopy(snap))
    monkeypatch.setattr(data_main, "write_objs_storage", _raise)
    with pytest.raises(RuntimeError):
        h.run_partial_cycle()
    assert h.snapshot_calls == []


# ----------------------------- refresh_teams ----------------------------------


class RefreshHarness:
    def __init__(
        self,
        monkeypatch,
        rename_254: bool = True,
        snapshot_available: bool = True,
    ):
        self.publish_calls: List[Any] = []
        self.snapshot_writes: List[Any] = []
        self.events: List[str] = []

        mp = monkeypatch
        mp.setattr(data_main, "DISABLE_GCS", False)

        self.tys = [
            ty(254, YEAR - 1, "fim", wins=10, losses=2, ties=0, count=12),
            ty(254, YEAR, "fim", wins=2, losses=1, ties=0, count=3),
            ty(1678, YEAR, None, wins=4, losses=0, ties=0, count=4),
        ]

        def consistent_team(num, name):
            wins = sum(t.wins for t in self.tys if t.team == num)
            losses = sum(t.losses for t in self.tys if t.team == num)
            count = sum(t.count for t in self.tys if t.team == num)
            last = max(t.year for t in self.tys if t.team == num)
            from src.data.wins import winrate

            return Team(
                team=num,
                name=name,
                rookie_year=2005,
                wins=wins,
                losses=losses,
                ties=0,
                count=count,
                winrate=winrate(wins, 0, count),
                last_active_year=last,
            )

        self.curr_teams = [
            consistent_team(254, "Old Name"),
            consistent_team(1678, "Citrus Circuits"),
        ]
        fresh_name = "The Cheesy Poofs" if rename_254 else "Old Name"
        self.fresh_teams = [
            Team(team=254, name=fresh_name, rookie_year=2005),
            Team(team=1678, name="Citrus Circuits", rookie_year=2005),
        ]

        mp.setattr(
            data_main, "load_teams_tba", lambda cache: deepcopy(self.fresh_teams)
        )
        mp.setattr(data_main, "get_teams_db", lambda: deepcopy(self.curr_teams))
        mp.setattr(data_main, "get_team_years_db", lambda **kw: deepcopy(self.tys))

        # refresh_teams prefers the snapshot as its diff basis (the DuckDB
        # Parquet cache can be one generation stale); the snapshot's objs
        # carry the current-year TeamYears.
        self.snapshot_teams = deepcopy(self.curr_teams)
        snap_tys = {
            f"{t.team}_{t.year}": deepcopy(t) for t in self.tys if t.year == YEAR
        }
        snap_objs = ("year", snap_tys, {}, {}, {}, {})
        mp.setattr(
            data_main,
            "read_snapshot",
            lambda year: (
                (snap_objs, deepcopy(self.snapshot_teams))
                if snapshot_available
                else None
            ),
        )

        def fake_write_snapshot(year, objs, teams):
            self.events.append("snapshot")
            self.snapshot_writes.append((year, deepcopy(teams)))

        def fake_publish_team_artifacts(teams, changed, all_ty):
            self.events.append("publish")
            self.publish_calls.append(
                {"teams": deepcopy(teams), "changed": set(changed)}
            )

        mp.setattr(data_main, "write_snapshot", fake_write_snapshot)
        mp.setattr(data_main, "publish_team_artifacts", fake_publish_team_artifacts)


def test_refresh_teams_dbless_republishes_exactly_affected(monkeypatch):
    h = RefreshHarness(monkeypatch)
    result = data_main.refresh_teams()

    assert result["teams_updated"] == 1

    # Exactly one targeted publish: full teams list, only 254 changed.
    assert len(h.publish_calls) == 1
    call = h.publish_calls[0]
    assert call["changed"] == {254}
    by_num = {t.team: t for t in call["teams"]}
    assert by_num[254].name == "The Cheesy Poofs"
    assert by_num[1678].name == "Citrus Circuits"

    # Snapshot teams updated in place so the next partial cycle's baseline
    # matches the published blobs.
    assert len(h.snapshot_writes) == 1
    snap_year, snap_teams = h.snapshot_writes[0]
    assert snap_year == YEAR
    assert {t.team: t.name for t in snap_teams}[254] == "The Cheesy Poofs"


def test_refresh_teams_dbless_no_snapshot_falls_back_to_parquet(monkeypatch):
    # Snapshot unreadable: the Parquet-backed reads are the basis; blobs and
    # Parquet are still republished, only the snapshot write is skipped.
    h = RefreshHarness(monkeypatch, snapshot_available=False)
    result = data_main.refresh_teams()
    assert result["teams_updated"] == 1
    assert len(h.publish_calls) == 1
    assert h.publish_calls[0]["changed"] == {254}
    assert h.snapshot_writes == []


def test_refresh_teams_dbless_quiet_publishes_nothing(monkeypatch):
    h = RefreshHarness(monkeypatch, rename_254=False)
    result = data_main.refresh_teams()
    assert result["teams_updated"] == 0
    assert h.publish_calls == []
    assert h.snapshot_writes == []


def test_refresh_teams_dbless_publishes_blobs_before_snapshot(monkeypatch):
    # Invariant: blobs first, snapshot last. The snapshot is the change-gate
    # baseline later cycles diff against, so it may only record state whose
    # blobs are already published.
    h = RefreshHarness(monkeypatch)
    data_main.refresh_teams()
    assert h.events == ["publish", "snapshot"]


def test_refresh_teams_dbless_publish_failure_leaves_snapshot_behind(monkeypatch):
    # If publish_team_artifacts throws, the snapshot must NOT have been
    # written: a snapshot ahead of the published blobs makes the change gate
    # report "unchanged" forever (stale team blobs until a manual full cycle).
    # Snapshot behind is recoverable — the next refresh_teams re-detects the
    # diff against the old snapshot and republishes.
    import pytest

    h = RefreshHarness(monkeypatch)
    monkeypatch.setattr(data_main, "publish_team_artifacts", _raise)
    with pytest.raises(RuntimeError):
        data_main.refresh_teams()
    assert h.snapshot_writes == []


# ------------------- publish_team_artifacts (storage layer) -------------------


def test_publish_team_artifacts_targets_exactly_affected(monkeypatch):
    prev = Manifest(
        blobs={
            "events/all": "v2/events/all.aaa",
            "team/1678": "v2/team/1678.bbb",
            parquet_logical(YEAR, "matches"): "v2/parquet.ccc",
        }
    )
    plans: List[UploadPlan] = []
    monkeypatch.setattr(storage, "read_manifest", lambda: prev)
    monkeypatch.setattr(storage, "_publish", lambda plan: plans.append(plan))

    teams = [
        team(254, name="The Cheesy Poofs", active=True),
        team(1678, name="Citrus Circuits", active=True),
    ]
    tys = [ty(254, YEAR, "fim", norm_epa=1800.0), ty(1678, YEAR, None)]
    storage.publish_team_artifacts(teams, {254}, tys)

    assert len(plans) == 1
    plan = plans[0]
    # Renders exactly teams/all + the changed team blob.
    assert set(plan.legacy_uploads) == {"teams/all", "team/254"}
    # The teams Parquet table rides the same manifest write.
    assert parquet_logical(YEAR, "teams") in plan.manifest.blobs
    # Everything else is carried forward untouched.
    assert plan.manifest.blobs["events/all"] == "v2/events/all.aaa"
    assert plan.manifest.blobs["team/1678"] == "v2/team/1678.bbb"
    assert plan.manifest.blobs[parquet_logical(YEAR, "matches")] == "v2/parquet.ccc"


def test_publish_team_artifacts_team_blob_embeds_team_years(monkeypatch):
    import json
    import zlib

    prev = Manifest()
    plans: List[UploadPlan] = []
    monkeypatch.setattr(storage, "read_manifest", lambda: prev)
    monkeypatch.setattr(storage, "_publish", lambda plan: plans.append(plan))

    teams = [team(254, name="The Cheesy Poofs", active=True)]
    tys = [
        ty(254, YEAR - 1, "fim", norm_epa=1700.0),
        ty(254, YEAR, "fim", norm_epa=1800.0),
    ]
    storage.publish_team_artifacts(teams, {254}, tys)

    blob = json.loads(zlib.decompress(plans[0].legacy_uploads["team/254"]))
    years = [row["year"] for row in blob["team_years"]]
    assert years == [YEAR - 1, YEAR]
