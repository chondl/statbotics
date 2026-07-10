"""One-time historical Parquet backfill (DB -> parquet/{year}/*.parquet).

The historical DB was built with GCS writes disabled, so the per-year Parquet
that the DuckDB read layer serves does not exist yet. This reads the existing DB
rows for each year and exports them through the same reviewed writer the pipeline
uses for historical years (``src.google.parquet.write_parquet`` /
``build_parquet_uploads``). It does NOT recompute EPA and never writes the DB.

    python backfill_parquet.py                # all years 2002..CURR_YEAR
    python backfill_parquet.py 2015 2016      # specific years
    python backfill_parquet.py --force        # ignore the progress checkpoint
    python backfill_parquet.py --verify       # read each parquet back, count rows vs DB
"""

import io
import json
import sys
from typing import Dict, List, Set

import pyarrow.parquet as pq

from src.constants import CURR_YEAR
from src.db.read.event import get_events as get_events_db
from src.db.read.match import get_matches as get_matches_db
from src.db.read.team import get_teams as get_teams_db
from src.db.read.team_event import get_team_events as get_team_events_db
from src.db.read.team_year import get_team_years as get_team_years_db
from src.db.read.year import get_year as get_year_db
from src.google.parquet import parquet_logical, write_parquet
from src.google.storage import _bucket, read_manifest

PROGRESS_OBJECT = "backfill/parquet_progress.json"
SKIP_YEARS = {2021}
TABLES = ["team_years", "events", "team_events", "matches", "teams", "year"]


def _read_progress(bucket) -> Dict:
    try:
        raw = bucket.blob(PROGRESS_OBJECT).download_as_bytes()
        return json.loads(raw)
    except Exception:
        return {"completed_years": []}


def _write_progress(bucket, progress: Dict) -> None:
    blob = bucket.blob(PROGRESS_OBJECT)
    blob.cache_control = "no-cache"
    blob.upload_from_string(json.dumps(progress).encode("utf-8"), "application/json")


def _load_year_objs(year: int):
    """Build the pipeline objs tuple for a year from existing DB rows."""
    year_obj = get_year_db(year)
    if year_obj is None:
        return None, None
    team_years = {ty.pk(): ty for ty in get_team_years_db(year=year)}
    events = {e.pk(): e for e in get_events_db(year=year)}
    team_events = {te.pk(): te for te in get_team_events_db(year=year)}
    matches = {m.pk(): m for m in get_matches_db(year=year)}
    objs = (year_obj, team_years, events, team_events, matches, {})
    return objs, dict(counts=dict(
        team_years=len(team_years),
        events=len(events),
        team_events=len(team_events),
        matches=len(matches),
    ))


def backfill_year(year: int, teams) -> Dict:
    objs, meta = _load_year_objs(year)
    if objs is None:
        print(f"  {year}: no Year row, skipping")
        return {}
    write_parquet(year, objs, teams)
    counts = meta["counts"]
    print(
        f"  {year}: wrote parquet "
        f"(team_years={counts['team_years']}, events={counts['events']}, "
        f"team_events={counts['team_events']}, matches={counts['matches']}, "
        f"teams={len(teams)})"
    )
    return counts


def verify_year(year: int, teams) -> bool:
    """Read each parquet blob back via the manifest and compare row counts to DB."""
    objs, meta = _load_year_objs(year)
    if objs is None:
        return True
    manifest = read_manifest()
    if manifest is None:
        print(f"  {year}: VERIFY FAIL — no manifest")
        return False
    bucket = _bucket()
    expected = dict(meta["counts"])
    expected["teams"] = len(teams)
    expected["year"] = 1
    ok = True
    for table in TABLES:
        logical = parquet_logical(year, table)
        key = manifest.blobs.get(logical)
        if key is None:
            print(f"  {year}: VERIFY FAIL — {logical} not in manifest")
            ok = False
            continue
        raw = bucket.blob(key).download_as_bytes()
        n = pq.read_table(io.BytesIO(raw)).num_rows
        exp = expected[table]
        mark = "ok" if n == exp else "MISMATCH"
        if n != exp:
            ok = False
        print(f"  {year} {table}: parquet={n} db={exp} {mark}")
    return ok


def main(argv: List[str]) -> None:
    force = "--force" in argv
    verify = "--verify" in argv
    year_args = [int(a) for a in argv if a.isdigit()]
    years = year_args or [y for y in range(2002, CURR_YEAR + 1) if y not in SKIP_YEARS]

    bucket = _bucket()
    teams = get_teams_db()

    if verify:
        all_ok = True
        for year in years:
            if year in SKIP_YEARS:
                continue
            all_ok = verify_year(year, teams) and all_ok
        print("VERIFY: ALL OK" if all_ok else "VERIFY: FAILURES ABOVE")
        sys.exit(0 if all_ok else 1)

    progress = _read_progress(bucket)
    completed: Set[int] = set(progress.get("completed_years", []))

    for year in years:
        if year in SKIP_YEARS:
            continue
        if year in completed and not force:
            print(f"{year}: already complete, skipping")
            continue
        print(f"{year}: backfilling parquet...")
        backfill_year(year, teams)
        completed.add(year)
        progress["completed_years"] = sorted(completed)
        _write_progress(bucket, progress)

    print(f"Done. Parquet backfilled for years: {sorted(completed)}")


if __name__ == "__main__":
    main(sys.argv[1:])
