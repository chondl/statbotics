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
import sys
from typing import List

import pyarrow.parquet as pq

from src.constants import CURR_YEAR
from src.db.read.event import get_events as get_events_db
from src.db.read.match import get_matches as get_matches_db
from src.db.read.team import get_teams as get_teams_db
from src.db.read.team_event import get_team_events as get_team_events_db
from src.db.read.team_year import get_team_years as get_team_years_db
from src.db.read.year import get_year as get_year_db
from src.google.parquet import build_parquet_uploads, parquet_logical
from src.google.publish import content_hash, versioned_key
from src.google.storage import (
    IMMUTABLE_CACHE,
    _bucket,
    read_manifest,
    write_manifest,
)

SKIP_YEARS = {2021}
TABLES = ["team_years", "events", "team_events", "matches", "teams", "year"]


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


def backfill_years(years: List[int], teams, bucket) -> None:
    """Upload every year's Parquet blobs, then update the manifest EXACTLY ONCE.

    write_parquet does a per-call read-modify-write of the manifest, which races
    the manifest's 60s Cache-Control in a tight multi-year loop (stale reads drop
    additions). Uploading all content-addressed blobs first and folding every ref
    into a single terminal manifest write is atomic and cache-safe — the same
    "single manifest write" invariant the current-year cycle uses.
    """
    manifest = read_manifest()
    if manifest is None:
        print("ABORT: manifest unavailable; refusing to write a fresh one")
        sys.exit(1)
    blobs = dict(manifest.blobs)

    for year in years:
        objs, meta = _load_year_objs(year)
        if objs is None:
            print(f"  {year}: no Year row, skipping")
            continue
        for logical, data in build_parquet_uploads(year, objs, teams).items():
            digest = content_hash(data)
            key = versioned_key(logical, digest)
            if manifest.hash_for(logical) != digest or logical not in blobs:
                blob = bucket.blob(key)
                blob.cache_control = IMMUTABLE_CACHE
                blob.upload_from_string(data, "application/octet-stream")
            blobs[logical] = key
        c = meta["counts"]
        print(
            f"  {year}: staged parquet (team_years={c['team_years']}, "
            f"events={c['events']}, team_events={c['team_events']}, "
            f"matches={c['matches']}, teams={len(teams)})"
        )

    manifest.blobs = blobs
    write_manifest(manifest, bucket)
    print("Wrote manifest once with all staged Parquet refs.")


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
    verify = "--verify" in argv
    year_args = [int(a) for a in argv if a.isdigit()]
    years = [
        y
        for y in (year_args or range(2002, CURR_YEAR + 1))
        if y not in SKIP_YEARS
    ]

    bucket = _bucket()
    teams = get_teams_db()

    if verify:
        all_ok = True
        for year in years:
            all_ok = verify_year(year, teams) and all_ok
        print("VERIFY: ALL OK" if all_ok else "VERIFY: FAILURES ABOVE")
        sys.exit(0 if all_ok else 1)

    print(f"Backfilling Parquet for years: {years}")
    backfill_years(years, teams, bucket)
    print("Done.")


if __name__ == "__main__":
    main(sys.argv[1:])
