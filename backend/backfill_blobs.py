"""One-time historical blob backfill.

    python backfill_blobs.py                # all past years
    python backfill_blobs.py 2018 2019      # specific years
    python backfill_blobs.py --force        # ignore the progress checkpoint
"""

import json
import sys
from collections import defaultdict
from typing import Dict, List, Set

from src.constants import CURR_YEAR, HIST_EPOCH
from src.db.read.event import get_events as get_events_db
from src.db.read.match import get_matches as get_matches_db
from src.db.read.team import get_teams as get_teams_db
from src.db.read.team_event import get_team_events as get_team_events_db
from src.db.read.team_year import get_team_years as get_team_years_db
from src.db.read.year import get_year as get_year_db
from src.google.publish import Manifest
from src.google.storage import (
    _bucket,
    read_manifest,
    upload_historical,
    write_manifest,
)
from src.site.event import _read_event, _read_events
from src.site.team import _read_team_year
from src.site.team_year import _read_team_years

PROGRESS_OBJECT = "backfill/progress.json"
SKIP_YEARS = {2021}


def _read_progress(bucket) -> Dict:
    try:
        raw = bucket.blob(PROGRESS_OBJECT).download_as_bytes()
        return json.loads(raw)
    except Exception:
        return {"epoch": HIST_EPOCH, "completed_years": []}


def _write_progress(bucket, progress: Dict) -> None:
    blob = bucket.blob(PROGRESS_OBJECT)
    blob.cache_control = "no-cache"
    blob.upload_from_string(json.dumps(progress).encode("utf-8"), "application/json")


def backfill_year(year: int, bucket) -> int:
    year_obj = get_year_db(year)
    if year_obj is None:
        print(f"  {year}: no Year row, skipping")
        return 0

    team_years = get_team_years_db(year=year)
    events = get_events_db(year=year)
    matches = get_matches_db(year=year)
    team_events = get_team_events_db(year=year)
    teams_by_num = {t.team: t for t in get_teams_db()}

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

    written = 0

    if upload_historical(
        f"team_years/{year}", _read_team_years(year, year_obj, team_years), bucket
    ):
        written += 1
    if upload_historical(f"events/{year}", _read_events(year_obj, events), bucket):
        written += 1

    for event in events:
        payload = _read_event(
            year_obj,
            event,
            matches_by_event.get(event.key, []),
            team_events_by_event.get(event.key, []),
        )
        if upload_historical(f"event/{event.key}", payload, bucket):
            written += 1

    for ty in team_years:
        team_obj = teams_by_num.get(ty.team)
        if team_obj is None:
            continue
        payload = _read_team_year(
            year_obj,
            team_obj,
            ty,
            team_events_by_team.get(ty.team, []),
            matches_by_team.get(ty.team, []),
        )
        if upload_historical(f"team/{ty.team}/{year}", payload, bucket):
            written += 1

    return written


def _ensure_manifest_epoch(bucket) -> None:
    manifest = read_manifest()
    if manifest is None:
        manifest = Manifest(cycle="backfill", hist_epoch=HIST_EPOCH, blobs={})
    else:
        manifest.hist_epoch = HIST_EPOCH
    write_manifest(manifest, bucket)


def main(argv: List[str]) -> None:
    force = "--force" in argv
    year_args = [int(a) for a in argv if a.isdigit()]

    if year_args:
        years = year_args
    else:
        years = [y for y in range(2002, CURR_YEAR) if y not in SKIP_YEARS]

    bucket = _bucket()
    progress = _read_progress(bucket)
    if progress.get("epoch") != HIST_EPOCH:
        progress = {"epoch": HIST_EPOCH, "completed_years": []}
    completed: Set[int] = set(progress.get("completed_years", []))

    total = 0
    for year in years:
        if year in SKIP_YEARS:
            continue
        if year in completed and not force:
            print(f"{year}: already complete, skipping")
            continue
        print(f"{year}: backfilling...")
        written = backfill_year(year, bucket)
        total += written
        completed.add(year)
        progress["completed_years"] = sorted(completed)
        _write_progress(bucket, progress)
        print(f"{year}: {written} objects written")

    _ensure_manifest_epoch(bucket)
    print(f"Done. {total} objects written this run (epoch {HIST_EPOCH}).")


if __name__ == "__main__":
    main(sys.argv[1:])
