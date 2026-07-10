from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional
import zlib

from google.cloud import storage

from src.constants import CURR_YEAR, HIST_EPOCH, PROD
from src.data.utils import objs_type
from src.db.functions import get_noteworthy_matches, get_upcoming_matches
from src.db.read.event import get_events as get_events_db
from src.db.read.team import get_teams as get_teams_db
from src.db.read.team_year import get_team_years as get_team_years_db
from src.google.publish import (
    MANIFEST_OBJECT,
    Manifest,
    UploadPlan,
    historical_key,
    plan_uploads,
)
from src.site.event import _read_all_events, _read_events, _read_event
from src.site.match import _read_noteworthy_matches, _read_upcoming_matches
from src.site.team import _read_all_teams, _read_team
from src.site.team_year import _read_team_years

BUCKET_NAME = "site_v1" if PROD else "site_dev_v1"

IMMUTABLE_CACHE = "public, max-age=31536000, immutable"
MANIFEST_CACHE = "public, max-age=60"


def compress(data: Any) -> bytes:
    # start = datetime.now()
    json_bytes = json.dumps(data).encode("utf-8")
    compressed = zlib.compress(json_bytes)
    # print(
    #     f"Compressed data from {len(json_bytes)} to {len(compressed)} bytes in {datetime.now() - start}"
    # )
    return compressed


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
    blob.upload_from_string(manifest.to_json().encode("utf-8"), "application/json")


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
) -> None:
    year = CURR_YEAR
    year_obj = objs[0]

    rendered: Dict[str, bytes] = {}

    def add(object_name: str, data: Any) -> None:
        rendered[object_name] = compress(data)

    # teams/all
    teams = get_teams_db()
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
    add("events/all", _read_all_events(get_events_db()))

    # events/{CURR_YEAR}
    events = list(objs[2].values())
    add(f"events/{year}", _read_events(year_obj, events))

    # event/{event.key}
    event_to_matches = defaultdict(list)
    event_to_team_events = defaultdict(list)
    for m in objs[4].values():
        event_to_matches[m.event].append(m)
    for te in objs[3].values():
        event_to_team_events[te.event].append(te)
    for event in events:
        add(
            f"event/{event.key}",
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
    all_team_years = get_team_years_db()
    team_years_by_team: Dict[int, List[Any]] = defaultdict(list)
    for ty in all_team_years:
        team_years_by_team[ty.team].append(ty)
    teams_by_num = {t.team: t for t in teams}
    for num in {ty.team for ty in team_years}:
        team_obj = teams_by_num.get(num)
        if team_obj is None:
            continue
        add(f"team/{num}", _read_team(team_obj, team_years_by_team.get(num, [])))

    # noteworthy_matches/{year}
    noteworthy_matches = get_noteworthy_matches(
        year=year, country=None, state=None, district=None, elim=None, week=None
    )
    add(f"noteworthy_matches/{year}", _read_noteworthy_matches(noteworthy_matches))

    # upcoming_matches?limit=20&metric={predicted_time | max_epa | sum_epa | diff_epa}
    for metric in ["predicted_time", "max_epa", "sum_epa", "diff_epa"]:
        upcoming_matches = get_upcoming_matches(
            country=None,
            state=None,
            district=None,
            elim=None,
            minutes=-1,
            limit=20,
            metric=metric,
        )
        add(
            f"upcoming_matches.limit=20.metric={metric}",
            _read_upcoming_matches(upcoming_matches),
        )

    prev = read_manifest()
    cycle = datetime.now(timezone.utc).isoformat()
    plan = plan_uploads(rendered, prev, cycle, hist_epoch=HIST_EPOCH)
    _publish(plan)

    return


def upload_historical(logical_path: str, data: Any, bucket: Any = None) -> bool:
    bucket = bucket or _bucket()
    blob = bucket.blob(historical_key(HIST_EPOCH, logical_path))
    if blob.exists():
        return False
    blob.cache_control = IMMUTABLE_CACHE
    blob.upload_from_string(compress(data), "application/octet-stream")
    return True
