import gzip
import os
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, TypeVar

import orjson

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage
from src.constants import CURR_YEAR, HIST_EPOCH, PROD
from src.data.backend import get_events as get_events_db
from src.data.backend import get_team_years as get_team_years_db
from src.data.backend import get_teams as get_teams_db
from src.data.utils import nan_safe_eq, objs_type
from src.db.models.team import Team
from src.google.publish import (
    MANIFEST_OBJECT,
    VERSION_PREFIX,
    Manifest,
    UploadPlan,
    content_hash,
    historical_key,
    plan_uploads,
    versioned_key,
)
from src.site.backend import get_noteworthy_matches, get_upcoming_matches
from src.site.event import _read_all_events, _read_event, _read_events
from src.site.match import _read_noteworthy_matches, _read_upcoming_matches
from src.site.team import _read_all_teams, _read_team, _read_team_year
from src.site.team_year import _read_team_years

# GCS bucket names are globally unique, so staging cannot reuse site_v1 /
# site_dev_v1. GCS_BUCKET overrides the name for staging (e.g.
# statbotics-staging-site); prod/local fall back to the historical defaults.
BUCKET_NAME = os.getenv("GCS_BUCKET") or ("site_v1" if PROD else "site_dev_v1")

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
MANIFEST_CACHE = "public, max-age=60"

GC_GRACE_HOURS = 48
GC_BATCH_SIZE = 100


def compress(data: Any) -> bytes:
    # orjson for speed; OPT_NON_STR_KEYS coerces int dict keys (e.g. the
    # team_to_events blob) to strings exactly as stdlib json does. Wire format
    # is unchanged: zlib-compressed JSON with no Content-Encoding, decoded by
    # pako.inflate + JSON.parse on the frontend. Note orjson serializes NaN as
    # null (stdlib's NaN literal would break JSON.parse anyway), so site-blob
    # data must never contain NaN.
    json_bytes = orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS)
    return zlib.compress(json_bytes)


def _bucket() -> Any:
    return storage.Client().bucket(BUCKET_NAME)


def _upload_bytes(
    bucket: Any, object_name: str, data: bytes, cache_control: Optional[str]
) -> None:
    blob = bucket.blob(object_name)
    if cache_control is not None:
        blob.cache_control = cache_control
    blob.upload_from_string(data, "application/octet-stream")


def upload_file_to_gcs(data: Any, object_name: str) -> None:
    _upload_bytes(_bucket(), object_name, compress(data), None)


T = TypeVar("T")


def _best_effort(label: str, fn: Callable[[], T], default: T) -> T:
    try:
        return fn()
    except Exception as e:
        print(f"skipped {label}: {e}")
        return default


class ManifestReadError(Exception):
    """manifest.json exists (or may exist) but could not be read or parsed.
    Writers must abort: planning from an empty manifest would erase
    hist_epochs and every carried-forward blob entry."""


class ManifestConflictError(Exception):
    """update_manifest lost the generation race on every attempt."""


# Conditional-write retries after the first attempt (spec §9: bounded, then
# fail loudly into /info).
MANIFEST_RETRIES = 5
# update_manifest calls that exhausted their retries since process start.
manifest_conflicts = 0


def _parse_manifest(raw: bytes) -> Manifest:
    # The object is stored gzip-compressed with Content-Encoding: gzip; the
    # client normally transcodes, but accept raw gzip bytes too.
    try:
        if raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
        return Manifest.from_json(raw)
    except Exception as e:
        raise ManifestReadError(f"manifest unparseable: {e}") from e


def read_manifest(bucket: Any = None) -> Optional[Manifest]:
    """The live manifest; None only when it does not exist. Any other failure
    raises ManifestReadError (fail closed)."""
    try:
        bucket = bucket or _bucket()
        raw = bucket.blob(MANIFEST_OBJECT).download_as_bytes()
    except NotFound:
        return None
    except Exception as e:
        raise ManifestReadError(f"manifest read failed: {e}") from e
    return _parse_manifest(raw)


def _read_manifest_pinned(bucket: Any) -> Optional[Tuple[Manifest, int]]:
    """(manifest, generation) read pinned to one generation, (Manifest(), 0)
    when absent, or None when the pinned generation vanished (retry)."""
    try:
        live = bucket.get_blob(MANIFEST_OBJECT)
    except Exception as e:
        raise ManifestReadError(f"manifest metadata read failed: {e}") from e
    if live is None:
        return Manifest(), 0
    generation = live.generation
    try:
        raw = bucket.blob(MANIFEST_OBJECT, generation=generation).download_as_bytes()
    except NotFound:
        return None
    except Exception as e:
        raise ManifestReadError(f"manifest read failed: {e}") from e
    return _parse_manifest(raw), generation


def update_manifest(
    mutate: Callable[[Manifest], None], bucket: Any = None
) -> Manifest:
    """The only way manifest.json is written. Reads one pinned generation,
    applies ``mutate`` (must be idempotent: it is re-applied to a fresh copy
    on every retry), stamps cycle and the global hist_epoch, drops
    hist_epochs entries the global epoch supersedes, and writes with
    if_generation_match. Retries MANIFEST_RETRIES times on a lost race, then
    raises ManifestConflictError."""
    global manifest_conflicts
    bucket = bucket or _bucket()
    for _ in range(1 + MANIFEST_RETRIES):
        pinned = _read_manifest_pinned(bucket)
        if pinned is None:
            continue
        manifest, generation = pinned
        prev_epoch = manifest.hist_epoch
        mutate(manifest)
        manifest.cycle = datetime.now(timezone.utc).isoformat()
        manifest.hist_epoch = max(prev_epoch, HIST_EPOCH)
        manifest.hist_epochs = {
            y: e for y, e in manifest.hist_epochs.items() if e > manifest.hist_epoch
        }

        blob = bucket.blob(MANIFEST_OBJECT)
        blob.cache_control = MANIFEST_CACHE
        blob.content_encoding = "gzip"
        try:
            blob.upload_from_string(
                gzip.compress(manifest.to_json().encode("utf-8")),
                "application/json",
                if_generation_match=generation,
            )
        except PreconditionFailed:
            continue
        return manifest
    manifest_conflicts += 1
    raise ManifestConflictError(
        f"manifest write lost the generation race {1 + MANIFEST_RETRIES} times"
    )


def _publish(
    plan: UploadPlan, mutate: Optional[Callable[[Manifest], None]] = None
) -> Manifest:
    """Upload the plan's objects, then commit its manifest delta (plus any
    extra ``mutate``, applied after the delta) in one update_manifest."""
    bucket = _bucket()

    jobs: List[Any] = []
    for versioned, data in plan.uploads.items():
        jobs.append((versioned, data, IMMUTABLE_CACHE))
    for logical, data in plan.legacy_uploads.items():
        jobs.append((logical, data, None))

    if jobs:
        with ThreadPoolExecutor() as executor:
            list(executor.map(lambda job: _upload_bytes(bucket, *job), jobs))

    # manifest is written last so readers always resolve a complete blob set
    def apply(manifest: Manifest) -> None:
        plan.apply(manifest)
        if mutate is not None:
            mutate(manifest)

    return update_manifest(apply, bucket)


def _fold_parquet(
    plan: UploadPlan, prev: Optional[Manifest], parquet: Dict[str, bytes]
) -> None:
    """Add Parquet tables whose digest changed to the plan's uploads and
    manifest delta."""
    prev_manifest = prev or Manifest()
    for logical, data in parquet.items():
        digest = content_hash(data)
        if prev_manifest.hash_for(logical) != digest:
            key = versioned_key(logical, digest)
            plan.uploads[key] = data
            plan.blob_updates[logical] = key


def _team_render_set(
    curr_ty: Dict[int, Any],
    orig_ty: Optional[Dict[int, Any]],
    teams_by_num: Dict[int, Team],
    orig_teams_by_num: Optional[Dict[int, Team]],
    prev_blobs: Dict[str, str],
) -> Set[int]:
    """Team numbers whose team/{num} blob must be (re)rendered this cycle.

    A team blob is a pure function of (team row, that team's team_years).
    Historical team_years never change on partial cycles — reprocess-year
    chains a full current-year re-render in code (see
    docs/superpowers/rig/deploy/Makefile, REPROCESS_DRIVER) — so a blob can
    only change when the team's current-year TeamYear or its team row changed
    since cycle start, or when it was never published. Correctness rule: a
    blob must NEVER be stale — when a baseline is missing, render.
    """
    if orig_ty is None:
        # Full rebuild: no baseline, render every current-year team.
        return set(curr_ty)

    # Candidates span orig teams too: a team whose current-year TeamYear was
    # removed (e.g. TBA registration removal) must re-render to drop the year
    # row.
    candidates = set(curr_ty) | set(orig_ty)

    if orig_teams_by_num is None:
        # No team-row baseline (e.g. a partial cycle whose snapshot read
        # failed and fell back to DB team rows): cannot prove any team row
        # unchanged, so render every candidate.
        return candidates

    render: Set[int] = set()
    for num in candidates:
        if (
            f"team/{num}" not in prev_blobs  # never published
            or not nan_safe_eq(curr_ty.get(num), orig_ty.get(num))  # TY changed
            or not nan_safe_eq(
                teams_by_num.get(num), orig_teams_by_num.get(num)
            )  # team row changed
        ):
            render.add(num)
    return render


def write_objs(
    objs: objs_type,
    orig_objs: Optional[objs_type] = None,
    teams: Optional[List[Team]] = None,
    parquet: Optional[Dict[str, bytes]] = None,
    orig_teams: Optional[List[Team]] = None,
) -> None:
    year = CURR_YEAR
    year_obj = objs[0]

    rendered: Dict[str, bytes] = {}

    def add(object_name: str, data: Any) -> None:
        rendered[object_name] = compress(data)

    # teams/all
    if teams is None:
        teams = _best_effort("teams", get_teams_db, [])
    add("teams/all", _read_all_teams(teams))

    # team_years/{CURR_YEAR}
    team_years = list(objs[1].values())
    add(f"team_years/{year}", _read_team_years(year, year_obj, team_years))

    # team_years/{CURR_YEAR}?limit=100&metric=epa
    top_team_years = sorted(team_years, key=lambda x: -x.epa)[:100]
    add(
        f"team_years/{year}.limit=100.metric=epa",
        _read_team_years(year, year_obj, top_team_years),
    )

    # events/all
    all_events = _best_effort("events/all", get_events_db, None)
    if all_events is not None:
        add("events/all", _read_all_events(all_events))

    # events/{CURR_YEAR}
    events = list(objs[2].values())
    add(f"events/{year}", _read_events(year_obj, events))

    # event/{event.key}
    # A ManifestReadError propagates and aborts the cycle: the event and team
    # gates would otherwise treat every blob as unpublished.
    prev = read_manifest()
    prev_blobs = prev.blobs if prev else {}
    event_to_matches = defaultdict(list)
    event_to_team_events = defaultdict(list)
    for m in objs[4].values():
        event_to_matches[m.event].append(m)
    for te in objs[3].values():
        event_to_team_events[te.event].append(te)

    orig_events = orig_objs[2] if orig_objs else {}
    orig_matches: Dict[str, List[Any]] = defaultdict(list)
    orig_team_events: Dict[str, List[Any]] = defaultdict(list)
    if orig_objs is not None:
        for m in orig_objs[4].values():
            orig_matches[m.event].append(m)
        for te in orig_objs[3].values():
            orig_team_events[te.event].append(te)

    for event in events:
        logical = f"event/{event.key}"
        event_changed = (
            not nan_safe_eq(event, orig_events.get(event.pk()))
            or not nan_safe_eq(event_to_matches[event.key], orig_matches[event.key])
            or not nan_safe_eq(
                event_to_team_events[event.key], orig_team_events[event.key]
            )
        )
        if event_changed or logical not in prev_blobs:
            add(
                logical,
                _read_event(
                    year_obj,
                    event,
                    event_to_matches.get(event.key, []),
                    event_to_team_events.get(event.key, []),
                ),
            )

    # team_to_events
    team_to_events = defaultdict(list)
    for team_event in objs[3].values():
        team_to_events[team_event.team].append(team_event.event)
    add("team_to_events", team_to_events)

    # team/{team.team} — gated: on partial cycles only render teams whose blob
    # could have changed (current-year TeamYear or team row vs cycle start, or
    # never published). Full cycles (orig_objs None) render everything.
    curr_ty = {ty.team: ty for ty in team_years}
    orig_ty = (
        None if orig_objs is None else {ty.team: ty for ty in orig_objs[1].values()}
    )
    teams_by_num = {t.team: t for t in teams}
    orig_teams_by_num = None if orig_teams is None else {t.team: t for t in orig_teams}
    render_nums = _team_render_set(
        curr_ty, orig_ty, teams_by_num, orig_teams_by_num, prev_blobs
    )
    if render_nums:
        # The all-years fetch exists only to feed team blobs; skip it entirely
        # on quiet cycles where the gate renders none.
        all_team_years = _best_effort("team pages", get_team_years_db, None)
        if all_team_years is not None:
            team_years_by_team: Dict[int, List[Any]] = defaultdict(list)
            for ty in all_team_years:
                if ty.year != year:
                    team_years_by_team[ty.team].append(ty)
            for ty in team_years:
                team_years_by_team[ty.team].append(ty)
            for num in render_nums:
                team_obj = teams_by_num.get(num)
                if team_obj is None:
                    continue
                add(
                    f"team/{num}", _read_team(team_obj, team_years_by_team.get(num, []))
                )

    # noteworthy_matches/{year}
    noteworthy_matches = _best_effort(
        "noteworthy_matches",
        lambda: get_noteworthy_matches(
            year=year, country=None, state=None, district=None, elim=None, week=None
        ),
        None,
    )
    if noteworthy_matches is not None:
        add(f"noteworthy_matches/{year}", _read_noteworthy_matches(noteworthy_matches))

    # upcoming_matches?limit=20&metric={predicted_time | max_epa | sum_epa | diff_epa}
    for metric in ["predicted_time", "max_epa", "sum_epa", "diff_epa"]:
        upcoming_matches = _best_effort(
            f"upcoming_matches.{metric}",
            lambda metric=metric: get_upcoming_matches(
                country=None,
                state=None,
                district=None,
                elim=None,
                minutes=-1,
                limit=20,
                metric=metric,
            ),
            None,
        )
        if upcoming_matches is not None:
            add(
                f"upcoming_matches.limit=20.metric={metric}",
                _read_upcoming_matches(upcoming_matches),
            )

    cycle = datetime.now(timezone.utc).isoformat()
    plan = plan_uploads(rendered, prev, cycle)
    if parquet:
        # Fold parquet into the same manifest so the cycle writes it exactly once,
        # last: DuckDB and the frontend never disagree by a cycle, and a partial
        # read can never clobber the site-blob references.
        _fold_parquet(plan, prev, parquet)
    _publish(plan)

    return


def publish_team_artifacts(
    teams: List[Team], changed_nums: Set[int], all_team_years: List[Any]
) -> None:
    """Targeted publish for db-less refresh_teams (DB retirement Phase 1 item
    4): renders teams/all plus team/{num} for exactly changed_nums, folds the
    current-year teams Parquet table into the same manifest write, and carries
    every other blob forward untouched. One manifest write, same invariants as
    write_objs (versioned uploads first, manifest last)."""
    # Local import: parquet.py imports this module at load time.
    from src.db.models.team import TeamORM
    from src.google.parquet import parquet_logical, serialize_table

    rendered: Dict[str, bytes] = {"teams/all": compress(_read_all_teams(teams))}

    team_years_by_team: Dict[int, List[Any]] = defaultdict(list)
    for ty in all_team_years:
        team_years_by_team[ty.team].append(ty)
    teams_by_num = {t.team: t for t in teams}
    for num in changed_nums:
        team_obj = teams_by_num.get(num)
        if team_obj is None:
            continue
        rendered[f"team/{num}"] = compress(
            _read_team(team_obj, team_years_by_team.get(num, []))
        )

    # A ManifestReadError propagates: refresh_teams aborts rather than
    # planning from an empty manifest.
    prev = read_manifest()
    cycle = datetime.now(timezone.utc).isoformat()
    plan = plan_uploads(rendered, prev, cycle)

    team_rows: List[Any] = list(teams)
    _fold_parquet(
        plan,
        prev,
        {parquet_logical(CURR_YEAR, "teams"): serialize_table(team_rows, TeamORM)},
    )

    _publish(plan)


def gc_versioned_blobs(
    grace_hours: int = GC_GRACE_HOURS, dry_run: bool = False
) -> Dict[str, Any]:
    bucket = _bucket()
    # Fail closed: without a readable manifest nothing is provably
    # unreferenced, so delete nothing.
    try:
        manifest = read_manifest(bucket)
    except ManifestReadError as e:
        print(f"GC {VERSION_PREFIX}/: refused, {e}")
        return {"status": "refused", "reason": str(e)}
    if manifest is None:
        print(f"GC {VERSION_PREFIX}/: refused, no manifest")
        return {"status": "refused", "reason": "no manifest"}
    referenced = set(manifest.blobs.values())
    cutoff = datetime.now(timezone.utc) - timedelta(hours=grace_hours)

    scanned = 0
    kept = 0
    freed = 0
    stale: List[Any] = []
    for blob in bucket.list_blobs(prefix=f"{VERSION_PREFIX}/"):
        scanned += 1
        created = blob.time_created
        if blob.name in referenced or (created is not None and created > cutoff):
            kept += 1
            continue
        stale.append(blob)
        freed += blob.size or 0

    if not dry_run:
        for i in range(0, len(stale), GC_BATCH_SIZE):
            bucket.delete_blobs(stale[i : i + GC_BATCH_SIZE])

    print(
        f"GC {VERSION_PREFIX}/: scanned {scanned}, kept {kept}, "
        f"deleted {len(stale)}{' (dry run)' if dry_run else ''}, freed {freed} bytes"
    )
    return {
        "scanned": scanned,
        "kept": kept,
        "deleted": len(stale),
        "bytes_freed": freed,
        "dry_run": dry_run,
    }


def upload_historical(logical_path: str, data: Any, bucket: Any = None) -> bool:
    bucket = bucket or _bucket()
    blob = bucket.blob(historical_key(HIST_EPOCH, logical_path))
    if blob.exists():
        return False
    blob.cache_control = IMMUTABLE_CACHE
    blob.upload_from_string(compress(data), "application/octet-stream")
    return True


def write_hist_blobs(year: int, objs: objs_type, teams: List[Team]) -> int:
    """Render + upload one historical year's hist/{epoch}/ site blobs from
    in-memory pipeline objects — the db-less replacement for the retired
    backfill_blobs.py operator script (DB retirement Phase 3), rendering
    exactly the object families it rendered, at the same paths:

        team_years/{year}    _read_team_years
        events/{year}        _read_events
        event/{key}          _read_event      (one per event)
        team/{num}/{year}    _read_team_year  (one per TeamYear with a
                                               known team row)

    Upload semantics are upload_historical's, unchanged: hist blobs are
    immutable within a HIST_EPOCH — objects that already exist are left
    alone. To force a full re-export (rendering or data changed for
    already-exported blobs), bump HIST_EPOCH in src/constants.py and rerun.
    The manifest is never touched here: its hist_epoch stamp is maintained
    by every manifest write (update_manifest stamps max(prev, HIST_EPOCH)),
    and hist objects are resolved by path, not through manifest.blobs.

    Uploads are independent single objects, so they run through a thread
    pool (the sequential exists+upload pairs dominated backfill_blobs.py's
    wall time; the bucket client is thread-safe, as _publish already relies
    on)."""
    # Input lists in the objs dicts' natural order, mirroring how
    # backfill_blobs consumed its DB reads: renderers that need a specific
    # order sort internally (_read_events/_read_event by time); the rest are
    # order-preserving, so payload bytes are a pure function of these lists.
    year_obj = objs[0]
    team_years = list(objs[1].values())
    events = list(objs[2].values())
    team_events = list(objs[3].values())
    matches = list(objs[4].values())
    teams_by_num = {t.team: t for t in teams}

    matches_by_event: Dict[str, List[Any]] = defaultdict(list)
    team_events_by_event: Dict[str, List[Any]] = defaultdict(list)
    for m in matches:
        matches_by_event[m.event].append(m)
    for te in team_events:
        team_events_by_event[te.event].append(te)

    matches_by_team: Dict[int, List[Any]] = defaultdict(list)
    for m in matches:
        for num in set(m.get_red()) | set(m.get_blue()):
            matches_by_team[num].append(m)
    team_events_by_team: Dict[int, List[Any]] = defaultdict(list)
    for te in team_events:
        team_events_by_team[te.team].append(te)

    payloads: List[Any] = [
        (f"team_years/{year}", _read_team_years(year, year_obj, team_years)),
        (f"events/{year}", _read_events(year_obj, events)),
    ]
    for event in events:
        payloads.append(
            (
                f"event/{event.key}",
                _read_event(
                    year_obj,
                    event,
                    matches_by_event.get(event.key, []),
                    team_events_by_event.get(event.key, []),
                ),
            )
        )
    for ty in team_years:
        team_obj = teams_by_num.get(ty.team)
        if team_obj is None:
            continue
        payloads.append(
            (
                f"team/{ty.team}/{year}",
                _read_team_year(
                    year_obj,
                    team_obj,
                    ty,
                    team_events_by_team.get(ty.team, []),
                    matches_by_team.get(ty.team, []),
                ),
            )
        )

    bucket = _bucket()
    with ThreadPoolExecutor() as executor:
        results = list(
            executor.map(lambda p: upload_historical(p[0], p[1], bucket), payloads)
        )
    written = sum(1 for r in results if r)
    print(f"hist blobs {year}: wrote {written}/{len(payloads)} (epoch {HIST_EPOCH})")
    return written
