from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import gzip
import os
from typing import Any, Callable, Dict, List, Optional, TypeVar
import zlib

from google.cloud import storage
import orjson

from src.constants import CURR_YEAR, HIST_EPOCH, PROD
from src.data.backend import (
    get_events as get_events_db,
    get_team_years as get_team_years_db,
    get_teams as get_teams_db,
)
from src.data.utils import nan_safe_eq, objs_type
from src.db.models.team import Team
from src.site.backend import get_noteworthy_matches, get_upcoming_matches
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
from src.site.event import _read_all_events, _read_events, _read_event
from src.site.match import _read_noteworthy_matches, _read_upcoming_matches
from src.site.team import _read_all_teams, _read_team
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


def read_manifest() -> Optional[Manifest]:
    try:
        raw = _bucket().blob(MANIFEST_OBJECT).download_as_bytes()
    except Exception:
        return None
    try:
        return Manifest.from_json(raw)
    except Exception:
        return None


def write_manifest(manifest: Manifest, bucket: Any = None) -> None:
    bucket = bucket or _bucket()
    blob = bucket.blob(MANIFEST_OBJECT)
    blob.cache_control = MANIFEST_CACHE
    blob.content_encoding = "gzip"
    blob.upload_from_string(
        gzip.compress(manifest.to_json().encode("utf-8")), "application/json"
    )


def _publish(plan: UploadPlan) -> None:
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
    write_manifest(plan.manifest, bucket)


def write_objs(
    objs: objs_type,
    orig_objs: Optional[objs_type] = None,
    teams: Optional[List[Team]] = None,
    parquet: Optional[Dict[str, bytes]] = None,
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

    # team/{team.team}
    all_team_years = _best_effort("team pages", get_team_years_db, None)
    if all_team_years is not None:
        team_years_by_team: Dict[int, List[Any]] = defaultdict(list)
        for ty in all_team_years:
            if ty.year != year:
                team_years_by_team[ty.team].append(ty)
        for ty in team_years:
            team_years_by_team[ty.team].append(ty)
        teams_by_num = {t.team: t for t in teams}
        for num in {ty.team for ty in team_years}:
            team_obj = teams_by_num.get(num)
            if team_obj is None:
                continue
            add(f"team/{num}", _read_team(team_obj, team_years_by_team.get(num, [])))

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
    plan = plan_uploads(rendered, prev, cycle, hist_epoch=HIST_EPOCH)
    if parquet:
        # Fold parquet into the same manifest so the cycle writes it exactly once,
        # last: DuckDB and the frontend never disagree by a cycle, and a partial
        # read can never clobber the site-blob references.
        prev_manifest = prev or Manifest()
        for logical, data in parquet.items():
            digest = content_hash(data)
            key = versioned_key(logical, digest)
            if prev_manifest.hash_for(logical) != digest:
                plan.uploads[key] = data
            plan.manifest.blobs[logical] = key
    _publish(plan)

    return


def gc_versioned_blobs(
    grace_hours: int = GC_GRACE_HOURS, dry_run: bool = False
) -> Dict[str, Any]:
    bucket = _bucket()
    manifest = read_manifest()
    referenced = set(manifest.blobs.values()) if manifest else set()
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
