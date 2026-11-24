import threading
import time
import traceback
from collections import defaultdict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

import requests

from src.constants import BACKEND_URL, CURR_YEAR, DISABLE_GCS
from src.data import trailing
from src.data.pipeline_lock import try_hold
from src.data.avg import process_year as process_year_avg
from src.data.backend import get_team_event_teams as get_team_event_teams_db
from src.data.backend import get_team_years as get_team_years_db
from src.data.backend import get_teams as get_teams_db
from src.data.epa.main import post_process as post_process_epa
from src.data.epa.main import process_year as process_year_epa
from src.data.tba import load_teams as load_teams_tba
from src.data.tba import process_year as process_year_tba
from src.data.teams import derive_team_districts, prune_teams
from src.data.utils import Timer, create_objs, objs_type
from src.data.wins import post_process as post_process_wins
from src.data.wins import process_year as process_year_wins
from src.data.wins import winrate
from src.db.models import Team, TeamYear
from src.google.parquet import build_parquet_uploads, write_parquet
from src.google.snapshot import read_snapshot, write_snapshot
from src.google.storage import publish_team_artifacts, render_site_blobs
from src.google.storage import read_manifest, update_manifest
from src.google.storage import write_objs as write_objs_storage
from src.google.trailing_publish import (
    chain_pending_now,
    stamp_trailing_bootstrap,
    trailing_publish,
    write_trailing_status,
)
from src.google.year_epoch import publish_past_year
from src.google.year_path import year_from_path
from src.tba import cache as tba_cache
from src.tba.clean_data import clean_district

# Set true when a db-less cross-season EPA seed found no prior-year data (a
# partial/forgotten Parquet backfill); surfaced via /info so it is not silent.
seed_incomplete = False

# Set true when a db-less full cycle skipped publish_curr_year because its
# teams never went through post_process (the all-years team_years read
# failed); cleared when a later full cycle publishes successfully. Surfaced
# via /info so it is not silent.
publish_skipped = False

# Set true when a db-less partial cycle found no readable snapshot (e.g. a
# transient GCS failure) and was skipped entirely — see the guard in
# _update_curr_year. Cleared by the next partial cycle that reads the
# snapshot successfully. Surfaced via /info so it is not silent.
partial_skipped = False

# update_curr_year's return value when PIPELINE_LOCK is held by another
# pipeline and the cycle was skipped; the endpoints answer {"status": "busy"}.
BUSY = "busy"


@contextmanager
def _tba_force_refresh(refresh_tba: bool) -> Iterator[None]:
    """Scope the REFRESH_TBA force flag (TBA cache design §2.3) to one
    pipeline run: when set, every get_tba call fetches unconditionally
    (etag-less) and the archives are rebuilt from the responses. Always
    cleared afterward so later runs in this process are not forced. The
    REFRESH_TBA=1 env var is honored independently inside
    tba_cache.force_refresh() (it covers job drivers calling process_year
    directly)."""
    tba_cache.set_force_refresh(refresh_tba)
    try:
        yield
    finally:
        tba_cache.set_force_refresh(False)


def process_year(
    year_num: int,
    partial: bool,
    tba_partial: bool,
    cache: bool,
    teams: List[Team],
    objs: objs_type,
    all_team_years: Optional[Dict[int, Dict[int, TeamYear]]],
    team_baseline_valid: bool = True,
) -> Tuple[List[Team], objs_type]:
    timer = Timer()
    curr_year_gcs = year_num == CURR_YEAR and not DISABLE_GCS
    # Warm the local TBA cache from GCS (year + global archives) before any
    # fetch. Once per archive per process; never raises; zero TBA requests.
    tba_cache.hydrate(year_num)
    orig_objs = deepcopy(objs)
    # Team-blob change-gate baseline: captured at cycle start, before anything
    # (process_year_tba today, or a future post-post_process teams publish)
    # mutates the teams list, so write_objs_storage compares against true
    # cycle-start team rows. Partial cycles only, mirroring orig_objs below.
    #
    # team_baseline_valid=False marks a partial cycle whose teams list did NOT
    # come from the snapshot the published blobs were rendered from. Those rows
    # may already carry mutations (refresh_teams, post_process) made since the
    # blobs were last rendered, so a baseline deepcopied from them would
    # wrongly report "unchanged" for stale blobs; orig_teams=None makes
    # write_objs_storage render every team. Defence in depth since Phase 4: a
    # partial cycle that cannot read the snapshot now returns early from
    # _update_curr_year (there is no DB to reload from), so partial always
    # implies a valid baseline in practice.
    orig_teams = deepcopy(teams) if partial and team_baseline_valid else None
    if all_team_years is None:
        all_team_years = defaultdict(dict)
        try:
            for year in range(max(2002, year_num - 4), year_num):
                for ty in get_team_years_db(year=year):
                    all_team_years[ty.year][ty.team] = ty
        except Exception:
            traceback.print_exc()
        if year_num > 2002 and not all_team_years:
            global seed_incomplete
            seed_incomplete = True
            print(
                "WARNING: db-less mode found no prior-year team_years for "
                f"{year_num}; every team will regress to the rookie mean. Back-fill "
                "prior years to Parquet BEFORE running db-less or EPA seeds are wrong."
            )

    new_teams, objs = process_year_tba(year_num, teams, objs, tba_partial, cache)
    teams += new_teams
    timer.print(str(year_num) + " TBA")

    year_obj = process_year_avg(objs[0], list(objs[4].values()))
    timer.print(str(year_num) + " AVG")

    objs = (year_obj, *objs[1:])

    objs = process_year_wins(objs)
    timer.print(str(year_num) + " Wins")

    objs = process_year_epa(objs, all_team_years)
    timer.print(str(year_num) + " EPA")

    # Partial cycles publish here. Full cycles DEFER the GCS publish (snapshot,
    # Parquet, site blobs) to publish_curr_year(), which the caller runs AFTER
    # post_process so post-processed Team fields — career records, norm_epa*,
    # active, last_active_year, district — reach GCS (DB retirement Phase 1
    # item 1). The team-blob change gate is unaffected: its orig_teams baseline
    # exists only on partial cycles (captured at cycle start above); full
    # cycles publish with orig=None → render-all.
    if curr_year_gcs and partial:
        # INVARIANT: blobs/manifest first, snapshot last (same gate-baseline
        # rule as refresh_teams and publish_curr_year). The snapshot is the
        # change-gate baseline the NEXT partial cycle diffs against, so it may
        # only record state whose blobs are already published. A storage
        # failure leaves the snapshot behind — the next cycle re-diffs against
        # the old baseline and over-renders (safe). Snapshot-first would leave
        # the baseline ahead of unpublished blobs on failure, and the gate
        # would mask them as "unchanged" forever. write_snapshot is a pure
        # serialize-and-upload of the in-memory objs/teams, and
        # write_objs_storage mutates neither, so the swap has no data
        # dependency in either direction.
        parquet_uploads = build_parquet_uploads(year_num, objs, teams)
        write_objs_storage(objs, orig_objs, teams, parquet_uploads, orig_teams)
        timer.print(str(year_num) + " Write Storage")

        write_snapshot(year_num, objs, teams)
        timer.print(str(year_num) + " Write Snapshot")

    # Current-year parquet is folded into the site manifest above; past years
    # publish parquet on their own manifest write. A past year's hist/ site
    # blobs are rendered from the same pipeline objects and published through
    # its per-year hist epoch (year_epoch.publish_past_year): unchanged pages
    # write nothing, a cold year fills gaps under the global epoch, and any
    # changed page moves the whole year to a fresh epoch, so a reprocess can
    # never leave a past-year page stale. A live year (the trailing year while
    # its window is open) never reaches this branch: publish_past_year
    # refuses it.
    #
    # A live non-current year (the trailing year while its window is open)
    # publishes like the current year instead: its site blobs and Parquet go
    # out as versioned objects in ONE manifest write (the trailing publish),
    # and the snapshot follows (blobs/manifest first, snapshot last). No hist
    # blob is written for it. Both branches set the manifest's chain_pending
    # in the write that changes parquet/{year}/team_years.
    live_year = trailing.is_live_year(year_num)
    if not curr_year_gcs and not DISABLE_GCS and live_year:
        trailing_publish(year_num, objs, teams)
        timer.print(str(year_num) + " Trailing Publish")

        write_snapshot(year_num, objs, teams)
        timer.print(str(year_num) + " Write Snapshot")

    if not curr_year_gcs and not DISABLE_GCS and not live_year:
        write_parquet(year_num, objs, teams)
        timer.print(str(year_num) + " Write Parquet")

        publish_past_year(year_num, render_site_blobs(year_num, objs, teams))
        timer.print(str(year_num) + " Write Hist Blobs")

    # Re-pack + upload only dirty TBA cache archives now that the year's
    # outputs (snapshot / storage / parquet) are written. Never raises.
    tba_cache.persist()

    return teams, objs


def post_process(
    teams: List[Team], all_team_years: Optional[Dict[int, Dict[int, TeamYear]]]
) -> List[Team]:
    timer = Timer()

    if all_team_years is None:
        all_team_years = defaultdict(dict)
        all_team_years_list = get_team_years_db()
        for ty in all_team_years_list:
            all_team_years[ty.year][ty.team] = ty

    teams = post_process_wins(teams, all_team_years)
    timer.print("Post Wins")

    teams = post_process_epa(teams, all_team_years)
    timer.print("Post EPA")

    # In-pipeline district derivation (DB retirement Phase 1 item 2): replaced
    # the DB-side update_team_districts, deleted in Phase 4.
    teams = derive_team_districts(teams, all_team_years)
    timer.print("Post District")

    return teams


def _clear_chain_pending(manifest: Any) -> None:
    manifest.chain_pending = False


def publish_curr_year(objs: objs_type, teams: List[Team]) -> List[Team]:
    """Full-cycle current-year GCS publish (snapshot, Parquet, site blobs),
    run AFTER post_process so the Team fields it mutates always reach GCS —
    in both DB and db-less modes (DB retirement Phase 1 item 1).

    Renders with orig baselines None (full render-all): the team-blob change
    gate only gates partial cycles, whose orig_teams baseline is captured at
    cycle start inside process_year — full cycles never had a baseline, so
    moving their publish after post_process cannot invalidate gating.

    Also applies the publish-time no-event prune (Phase 1 item 3): teams with
    zero TeamEvents across all years are excluded from teams/all and the
    teams Parquet — the replacement for the DB-side
    remove_teams_with_no_events (deleted in Phase 4), over the same TeamEvent
    set (stored years unioned with this cycle's in-memory team_events). If the
    stored set cannot be read the prune is skipped for the cycle: publishing
    an unpruned list is recoverable, silently dropping teams is not.

    Returns the published (pruned) teams list — the pipeline state that
    subsequent partial cycles resume from via the snapshot."""
    if DISABLE_GCS or objs[0].year != CURR_YEAR:
        return teams

    timer = Timer()
    try:
        event_teams: Optional[Set[int]] = set(get_team_event_teams_db())
    except Exception:
        traceback.print_exc()
        print("WARNING: stored TeamEvent teams unavailable; skipping no-event prune")
        event_teams = None
    if event_teams is not None:
        event_teams |= {te.team for te in objs[3].values()}
        teams = prune_teams(teams, event_teams)
        timer.print(str(CURR_YEAR) + " Prune Teams")

    # INVARIANT: blobs/manifest first, snapshot last — the snapshot written
    # here is the change-gate baseline the next partial cycle diffs against,
    # so it may only record state whose blobs are already published (see the
    # partial-cycle publish in process_year for the full reasoning).
    parquet_uploads = build_parquet_uploads(CURR_YEAR, objs, teams)
    # A full render of every team page: the chained current-year re-render a
    # trailing or past-year team_years change asked for (chain_pending) is
    # done, in this same manifest write.
    write_objs_storage(
        objs, None, teams, parquet_uploads, None, mutate=_clear_chain_pending
    )
    timer.print(str(CURR_YEAR) + " Write Storage")

    write_snapshot(CURR_YEAR, objs, teams)
    timer.print(str(CURR_YEAR) + " Write Snapshot")

    return teams


def reset_all_years(refresh_tba: bool = False):
    with _tba_force_refresh(refresh_tba):
        _reset_all_years()


def _reset_all_years():
    timer = Timer()

    start_year = 2002
    end_year = CURR_YEAR

    teams = load_teams_tba(cache=True)
    timer.print("Load Teams")

    all_team_years: Dict[int, Dict[int, TeamYear]] = {}
    curr_objs: Optional[objs_type] = None
    for year_num in range(start_year, end_year + 1):
        objs = create_objs(year_num)
        if year_num == 2021:
            continue

        teams, objs = process_year(
            year_num, False, False, year_num < CURR_YEAR, teams, objs, all_team_years
        )
        all_team_years[year_num] = {ty.team: ty for ty in objs[1].values()}
        if year_num == CURR_YEAR:
            curr_objs = objs

    teams = post_process(teams, all_team_years)

    # Full cycles publish the current year AFTER post_process (Phase 1 item 1)
    # so post-processed Team fields reach the snapshot/Parquet/blobs.
    if curr_objs is not None:
        publish_curr_year(curr_objs, teams)

    # Catch-all for anything dirtied after the last per-year persist (e.g.
    # the global teams archive written by load_teams on a cold start).
    tba_cache.persist()


def update_curr_year(
    partial: bool, tba_partial: bool, refresh_tba: bool = False
) -> Optional[str]:
    """Returns BUSY when another pipeline holds the lock (nothing ran),
    else None."""
    with _tba_force_refresh(refresh_tba):
        return _update_curr_year(partial, tba_partial)


def _update_curr_year(partial: bool, tba_partial: bool) -> Optional[str]:
    with try_hold() as held:
        if not held:
            print("PIPELINE BUSY: skipped update_curr_year")
            return BUSY
        _update_curr_year_held(partial, tba_partial)
        return None


def _update_curr_year_held(partial: bool, tba_partial: bool):
    year = CURR_YEAR
    timer = Timer()

    objs: Optional[objs_type] = None
    teams: Optional[List[Team]] = None
    snapshot_loaded = False
    if partial and not DISABLE_GCS:
        loaded = read_snapshot(year)
        if loaded is not None:
            objs, teams = loaded
            snapshot_loaded = True
            timer.print("Read Snapshot")

    global partial_skipped
    if objs is None or teams is None:
        if partial:
            # INVARIANT (db-less): a partial cycle may only run from the
            # snapshot. There is no other source of prior pipeline state —
            # the fallback objs below are EMPTY (create_objs), and the TBA
            # tier logic skips out-of-window completed events whose manifest
            # validation is fresh, so a partial cycle seeded from empty objs
            # would publish near-empty artifacts over the good
            # snapshot/blobs/current-year Parquet. Skip the cycle entirely:
            # process nothing, publish nothing. The next successful snapshot
            # read (or an operator full reset) resumes normal operation.
            partial_skipped = True
            print(
                "ERROR: db-less partial cycle SKIPPED — snapshot "
                "unreadable, and running from empty objs would publish "
                "near-empty artifacts over good ones. Surfaced via /info "
                "PARTIAL_SKIPPED."
            )
            return
        teams = load_teams_tba(cache=True)
        objs = create_objs(year)
        timer.print("Load Teams (TBA)")
    elif partial:
        # Snapshot readable again: normal partial operation resumes.
        partial_skipped = False

    # On the snapshot-fallback path (partial but snapshot_loaded False), the
    # team-row baseline is invalid — see process_year for the reasoning.
    teams, objs = process_year(
        year,
        partial,
        tba_partial,
        year < CURR_YEAR,
        teams,
        objs,
        None,
        team_baseline_valid=snapshot_loaded,
    )

    if not partial:
        # Full cycle: the current-year publish is deferred until after
        # post_process (Phase 1 item 1). Current-year TeamYears come from
        # this cycle's in-memory objs — the publish that would persist them
        # has not happened yet. Prior years come from the backend (Parquet
        # via DuckDB). If that read fails, skip post_process rather than
        # publish half-computed career fields (see the invariant below).
        all_team_years: Optional[Dict[int, Dict[int, TeamYear]]] = None
        try:
            prior_tys = get_team_years_db()
        except Exception:
            traceback.print_exc()
            print(
                "WARNING: all-years team_years unavailable; skipping post_process "
                "this cycle"
            )
            prior_tys = None
        if prior_tys is not None:
            all_team_years = defaultdict(dict)
            for ty in prior_tys:
                if ty.year != year:
                    all_team_years[ty.year][ty.team] = ty
            all_team_years[year] = {ty.team: ty for ty in objs[1].values()}
            teams = post_process(teams, all_team_years)

        global publish_skipped
        if prior_tys is None:
            # INVARIANT (DB retirement Phase 1): a full cycle may only publish
            # teams that went through post_process. Here the teams list is
            # fresh from load_teams_tba — career records, norm_epa, active,
            # last_active_year, district all empty — and publishing it would
            # clobber good published blobs, the teams Parquet, AND the
            # snapshot partial cycles resume from. Skip the publish; the next
            # successful full cycle republishes everything.
            publish_skipped = True
            print(
                "ERROR: db-less full cycle SKIPPING publish_curr_year — teams "
                "never went through post_process (all-years team_years read "
                "failed), so publishing would clobber good artifacts with "
                "empty career fields. Surfaced via /info "
                "PUBLISH_SKIPPED."
            )
        else:
            publish_curr_year(objs, teams)
            publish_skipped = False


def reprocess_year(year_num: int) -> None:
    """Full single-year historical reprocess — the one driver for both the
    daily TBA sweep (design §2.3) and the operator `reprocess-year` job in
    docs/superpowers/rig/deploy/Makefile (whose REPROCESS_DRIVER calls this
    function directly). Needs no DB:

    - process_year(partial=False, cache=True): re-ingests the year from the
      freshly revalidated pickles (zero further TBA traffic for validated
      paths), recomputes EPA with the prior-4-year team_years seeding read
      inside process_year (all_team_years=None — Parquet via DuckDB),
      republishes the year's Parquet, and publishes its hist/ site blobs
      from the pipeline objects through the year's hist epoch
      (year_epoch.publish_past_year: a changed page advances the year).
    - Chained full current-year re-render: team/{num} site blobs embed each
      team's full-history team_years and the team-blob change gate skips
      them on partial cycles, so a historical reprocess MUST chain a full
      current-year re-render or team blobs go stale (see the
      REPROCESS_DRIVER note in the Makefile).

    Runs synchronously in-request like reset_curr_year; minutes, acceptable
    for the scheduled daily sweep.

    A live trailing year is not a past year: it delegates to a full
    trailing cycle (update_trailing_year), which publishes through the
    manifest and chains the current-year re-render itself when needed."""
    if year_num != CURR_YEAR and trailing.is_live_year(year_num):
        # Inline chain: a job driver's process exits when this returns, so
        # an HTTP self-call from a daemon thread would die with it.
        update_trailing_year(full=True, chain=CHAIN_INLINE)
        return

    timer = Timer()
    teams = load_teams_tba(cache=True)
    process_year(year_num, False, False, True, teams, create_objs(year_num), None)
    timer.print(f"{year_num} Reprocess")

    update_curr_year(partial=False, tba_partial=False)
    refresh_teams()
    timer.print("Chained Curr-Year Re-render")


def refresh_teams() -> Dict[str, Any]:
    """Refresh all stale team fields from TBA and recompute win records.

    DB retirement Phase 1 item 4: the staleness diff is computed from the
    backend reads (Parquet via DuckDB) and persisted by republishing exactly
    the affected artifacts — the current-year teams Parquet, teams/all, the
    changed team/{num} blobs, and the snapshot's teams. Scheduled daily
    (statbotics-refresh-teams) so no operator action is ever needed to keep
    team fields current with TBA."""
    with try_hold() as held:
        if not held:
            print("PIPELINE BUSY: skipped refresh_teams")
            return {"status": "busy"}
        return _refresh_teams_held()


def _refresh_teams_held() -> Dict[str, Any]:
    timer = Timer()

    fresh_teams = load_teams_tba(cache=False)
    fresh_team_map: Dict[int, Team] = {t.team: t for t in fresh_teams}
    timer.print("Fetch TBA Teams")

    # The snapshot (not the DuckDB Parquet cache) is the diff basis when
    # available: DuckDB's sync TTL can serve the pre-publish generation for up
    # to 30s, and on the chained reset_curr_year path that would clobber
    # just-published post-processed fields with stale ones. The snapshot is the
    # authoritative pipeline state the publish was rendered from. Current-year
    # TeamYears come from the same snapshot for the same reason; prior years
    # from Parquet (immutable between full cycles).
    snapshot_state = None
    if not DISABLE_GCS:
        snapshot_state = read_snapshot(CURR_YEAR)
    if snapshot_state is not None:
        snap_objs, snap_teams = snapshot_state
        db_teams = snap_teams
        all_team_years_list = [
            ty for ty in get_team_years_db() if ty.year != CURR_YEAR
        ] + list(snap_objs[1].values())
    else:
        db_teams = get_teams_db()
        all_team_years_list = get_team_years_db()
    timer.print("Fetch Teams + TeamYears")

    # Compute true last_active_year per team
    true_last: Dict[int, int] = {}
    for ty in all_team_years_list:
        if ty.team not in true_last or ty.year > true_last[ty.team]:
            true_last[ty.team] = ty.year

    # Compute aggregate win record per team from TeamYear data
    t_record: Dict[int, Tuple[int, int, int, int]] = defaultdict(lambda: (0, 0, 0, 0))
    for ty in all_team_years_list:
        t_record[ty.team] = (
            t_record[ty.team][0] + ty.wins,
            t_record[ty.team][1] + ty.losses,
            t_record[ty.team][2] + ty.ties,
            t_record[ty.team][3] + ty.count,
        )

    # Find teams with any stale fields
    teams_to_update: Dict[int, Team] = {}
    for t in db_teams:
        dirty = False
        fresh = fresh_team_map.get(t.team)

        if fresh is not None:
            for field in ("name", "country", "state", "rookie_year"):
                fresh_val = getattr(fresh, field)
                if fresh_val is not None and getattr(t, field) != fresh_val:
                    setattr(t, field, fresh_val)
                    dirty = True

        cleaned = clean_district(t.district)
        if cleaned != t.district:
            t.district = cleaned
            dirty = True

        if true_last.get(t.team) != t.last_active_year:
            t.last_active_year = true_last.get(t.team)
            dirty = True

        rec = t_record[t.team]
        if (t.wins, t.losses, t.ties, t.count) != rec:
            t.wins, t.losses, t.ties, t.count = rec
            t.winrate = winrate(t.wins, t.ties, t.count)
            dirty = True

        if dirty:
            teams_to_update[t.team] = t

    # Persistence: republish exactly the affected artifacts — the snapshot's
    # teams, the current-year teams Parquet, teams/all, and the changed
    # team/{num} blobs. The TeamYear/TeamEvent Parquet keeps its published
    # team-sourced fields until the next full cycle re-derives them from TBA —
    # matching the scope of what team pages render from team rows.
    if teams_to_update and not DISABLE_GCS:
        # INVARIANT: blobs first, snapshot last. The snapshot is the team-blob
        # change-gate baseline the next partial cycle diffs against, so it may
        # only record state whose blobs are already published. If the blob
        # publish throws, the snapshot stays behind — recoverable: the next
        # refresh_teams (or partial cycle, whose baseline still predates the
        # change) re-detects the diff and republishes. Snapshot-first would
        # leave the baseline AHEAD of unpublished blobs on failure, and the
        # gate would report "unchanged" forever (stale team blobs until a
        # manual full cycle).
        publish_team_artifacts(db_teams, set(teams_to_update), all_team_years_list)

        # The refreshed rows were mutated in place on the snapshot's teams
        # list (when a snapshot exists), so rewriting it brings the gate
        # baseline in step with the blobs just published.
        if snapshot_state is not None:
            write_snapshot(CURR_YEAR, snapshot_state[0], db_teams)
            timer.print("Write Snapshot")
    timer.print(f"Publish Teams ({len(teams_to_update)} rows)")
    return {"teams_updated": len(teams_to_update)}


# ------------------------------------------------------------------------
# Trailing year (trailing-year refresh spec §5.2, §6, §7)
# ------------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _secs(start: float) -> int:
    return int(round(time.monotonic() - start))


def _schedule_reset_curr_year() -> None:
    """Chain a full current-year re-render plus refresh_teams as its OWN
    request (GET /v3/data/reset_curr_year), from a daemon thread started
    after the pipeline lock is released. The short read timeout only frees
    this thread; the server-side request runs on."""

    def call() -> None:
        try:
            requests.get(f"{BACKEND_URL}/v3/data/reset_curr_year", timeout=(5, 5))
        except requests.exceptions.ReadTimeout:
            pass
        except Exception:
            traceback.print_exc()

    threading.Thread(target=call, name="chain-reset-curr-year", daemon=True).start()


CHAIN_HTTP = "http"
CHAIN_INLINE = "inline"


def _chain_inline() -> None:
    """The chained full current-year re-render, synchronously in this
    thread (the caller holds the lock; try_hold is re-entrant for it)."""
    update_curr_year(partial=False, tba_partial=False)
    refresh_teams()


def _run_with_chain(
    name: str, work: Any, chain: str
) -> Dict[str, Any]:
    if chain not in (CHAIN_HTTP, CHAIN_INLINE):
        raise ValueError(f"chain must be {CHAIN_HTTP!r} or {CHAIN_INLINE!r}")
    with try_hold() as held:
        if not held:
            print(f"PIPELINE BUSY: skipped {name}")
            return {"status": "busy"}
        result = work()
        if result.get("chain") and chain == CHAIN_INLINE:
            _chain_inline()
    # The HTTP chain is scheduled after the lock is released, or the chained
    # request would skip as busy.
    if result.get("chain") and chain == CHAIN_HTTP:
        _schedule_reset_curr_year()
    return result


def update_trailing_year(full: bool, chain: str = CHAIN_HTTP) -> Dict[str, Any]:
    """One trailing cycle for the trailing year (spec §5.2). Returns
    {"status": "busy"} without waiting when another pipeline holds the
    lock.

    ``chain`` picks how the current-year re-render runs when the manifest's
    chain_pending is set after the cycle (the trailing year's team_years
    changed, now or in a cycle whose chain never published): "http"
    (endpoints) schedules it as its own
    request; "inline" (job drivers such as reprocess_year, whose process
    exits when they return) runs it synchronously before returning."""
    return _run_with_chain(
        "update_trailing_year", lambda: _update_trailing_year_held(full), chain
    )


def _update_trailing_year_held(full: bool) -> Dict[str, Any]:
    year = trailing.trailing_year()
    if not trailing.is_live_year(year):
        print(f"TRAILING {year}: window closed; trailing cycle skipped")
        return {"status": "skipped", "reason": "window-closed", "year": year}
    if DISABLE_GCS:
        # Without GCS process_year skips the trailing publish, so there is
        # nothing to stamp and no snapshot to resume from.
        print(f"TRAILING {year}: DISABLE_GCS set; trailing cycle skipped")
        return {"status": "skipped", "reason": "gcs-disabled", "year": year}

    start = time.monotonic()
    objs: Optional[objs_type] = None
    teams: Optional[List[Team]] = None
    if not full:
        loaded = read_snapshot(year)
        if loaded is None:
            print(f"TRAILING {year}: snapshot unreadable; escalating to full")
            full = True
        else:
            objs, teams = loaded

    status: Dict[str, Any] = {
        "year": year,
        "state": "running",
        "full": full,
        "started": _now_iso(),
        "finished": None,
        "error": None,
    }
    write_trailing_status(status)
    try:
        if full or objs is None or teams is None:
            # Full trailing cycles fetch TBA fresh (no etag trust).
            teams = load_teams_tba(cache=False)
            objs = create_objs(year)
        # The live non-current branch of process_year runs the trailing
        # publish, then writes the trailing snapshot.
        process_year(year, not full, not full, not full, teams, objs, None)
        if full:
            stamp_trailing_bootstrap(year)
    except Exception as e:
        status.update(state="error", finished=_now_iso(), error=f"{type(e).__name__}: {e}")
        write_trailing_status(status)
        raise

    status.update(state="ok", finished=_now_iso())
    write_trailing_status(status)
    print(f"TRAILING {year} CYCLE COMPLETE full={int(full)} secs={_secs(start)}")
    return {
        "status": "success",
        "year": year,
        "full": full,
        # The manifest's chain_pending, set by this cycle's trailing publish
        # or left by an earlier one whose chain never published.
        "chain": chain_pending_now(),
    }


def close_needed(manifest: Optional[Any], year: int) -> bool:
    """Window close still has work: the manifest names a trailing year, or
    holds any non-Parquet year key of ``year``. (The window check is the
    caller's.)"""
    if manifest is None:
        return False
    if manifest.trailing_year is not None:
        return True
    return any(
        year_from_path(k) == year and not k.startswith("parquet/")
        for k in manifest.blobs
    )


def close_would_act(manifest: Optional[Any], year: int) -> bool:
    """Window close has work to do now: the trailing window is closed and
    close_needed holds. The gate and close_trailing_year share this rule."""
    return not trailing.trailing_window_open() and close_needed(manifest, year)


def close_trailing_year(chain: str = CHAIN_HTTP) -> Dict[str, Any]:
    """Window close (spec §7, with the plan's past-year deviation): retire
    the trailing year into an ordinary past year. Stateless and rerunnable:
    every step is idempotent, so a run that dies is completed by the next.
    ``chain`` as for update_trailing_year."""
    return _run_with_chain("close_trailing_year", _close_trailing_year_held, chain)


def _close_trailing_year_held() -> Dict[str, Any]:
    year = trailing.trailing_year()
    start = time.monotonic()
    if DISABLE_GCS:
        # Without GCS process_year writes no Parquet or hist blobs: clearing
        # the marker would retire the year with nothing published.
        print(f"TRAILING {year} CLOSE not_needed secs={_secs(start)}")
        return {"status": "not_needed", "year": year, "reason": "gcs-disabled"}

    # A ManifestReadError propagates: without the manifest, nothing is known.
    manifest = None if trailing.trailing_window_open() else read_manifest()
    if not close_would_act(manifest, year):
        print(f"TRAILING {year} CLOSE not_needed secs={_secs(start)}")
        return {"status": "not_needed", "year": year}
    assert manifest is not None

    status: Dict[str, Any] = {
        "year": year,
        "state": "running",
        "full": True,
        "started": _now_iso(),
        "finished": None,
        "error": None,
    }
    write_trailing_status(status)
    try:
        # Final full cycle with PAST-year semantics (the window is closed, so
        # the year is no longer live): fresh TBA, then the past-year branch —
        # write_parquet, then publish_past_year, which advances the year
        # epoch on any change and removes the year's non-Parquet keys.
        # write_parquet sets chain_pending if team_years changed.
        teams = load_teams_tba(cache=False)
        process_year(year, False, False, False, teams, create_objs(year), None)

        def clear(m: Any) -> None:
            m.trailing_year = None

        chain = update_manifest(clear).chain_pending
    except Exception as e:
        status.update(state="error", finished=_now_iso(), error=f"{type(e).__name__}: {e}")
        write_trailing_status(status)
        print(f"TRAILING {year} CLOSE error secs={_secs(start)}")
        raise

    status.update(state="ok", finished=_now_iso())
    write_trailing_status(status)
    print(f"TRAILING {year} CLOSE closed secs={_secs(start)}")
    return {"status": "closed", "year": year, "chain": chain}
