"""Daily historical-year TBA revalidation sweep (cache design §2.3).

Cloud Scheduler hits /v3/data/revalidate_tba once a day; each hit sweeps
exactly ONE historical year — round-robin over 2002..CURR_YEAR-1 with 2021
skipped — using SERIAL conditional GETs: one request in flight, ever, per
the 2026-07-21 user decision on TBA politeness. Never parallelize this. The
round-robin cursor lives in the global archive's manifest (reserved
__sweep__ key) so it survives process restarts and redeploys.

Change handling: a 304 refreshes last_validated only. A 200 rewrites the
local pickle immediately, but the new etag is recorded in the manifest ONLY
after the year's full reprocess succeeds — a failed reprocess leaves the old
etag stored, so the next visit of the year re-detects the change and retries.
Revalidation can therefore never update the stored cache while leaving
published data (Parquet / DB / blobs) stale.

Failure honesty: per-path errors and a failed reprocess are collected into
the response (status "error") but never raise, and archives are only
persisted through tba_cache.persist()'s never-raise path.
"""

import os
import traceback
from typing import Any, Dict, List, Optional, Tuple

import src.data.main as data_main
import src.tba.main as tba_main
from src.constants import CURR_YEAR
from src.tba import cache as tba_cache
from src.tba.utils import dump_cache

SWEEP_START_YEAR = 2002
SKIP_YEARS = {2021}  # season cancelled; reset_all_years skips it too (§4)


def normalize_cursor(year: Optional[int]) -> int:
    """Clamp a stored cursor to a sweepable historical year: missing or
    out-of-range restarts at the oldest year; skip years advance."""
    if year is None or year < SWEEP_START_YEAR or year >= CURR_YEAR:
        year = SWEEP_START_YEAR
    while year in SKIP_YEARS:
        year += 1
    return year


def next_sweep_year(year: int) -> int:
    """Round-robin successor: +1, skipping 2021, wrapping from CURR_YEAR-1
    back to the oldest year."""
    return normalize_cursor(year + 1)


def revalidate_tba() -> Dict[str, Any]:
    """Sweep one historical year; reprocess it if anything changed."""
    tba_cache.hydrate()  # global archive: the cursor (+ teams etag state)
    year = normalize_cursor(tba_cache.sweep_cursor())
    tba_cache.hydrate(year)

    result: Dict[str, Any] = {"status": "success", "year": year, "reprocessed": False}
    errors: List[str] = []

    paths = tba_cache.manifest_paths(str(year))
    if not paths:
        # Cold year: no manifest means nothing to revalidate (a full-history
        # rebuild seeds it). The cursor still advances — one year per day.
        print(f"TBA sweep: no manifest for {year} (cold); skipping")
        result["status"] = "skipped"
    else:
        changed: List[Tuple[str, Optional[str]]] = []
        for url in paths:  # SERIAL by design — see module docstring
            etag = tba_cache.stored_etag(url)
            try:
                data, new_etag = tba_main._get_tba(url, etag)
            except Exception:
                traceback.print_exc()
                errors.append(url)
                continue
            if data is True:
                # 304: the stored pickle is still current.
                tba_cache.record_not_modified(url, new_etag)
            elif data is False:
                # Non-200/304 from TBA: loud, but state untouched.
                errors.append(url)
            elif dump_cache(os.path.join(tba_main.TBA_CACHE_DIR, url), data):
                # 200: the pickle now holds the new data. Recording the etag
                # is deferred until the reprocess lands (module docstring).
                changed.append((url, new_etag))
            else:
                errors.append(url)  # pickle write failed; etag not recorded

        result["checked"] = len(paths)
        result["changed"] = len(changed)
        result["changed_paths"] = [url for url, _ in changed]
        if changed:
            print(f"TBA sweep: {year} changed ({len(changed)} paths); reprocessing")
            try:
                data_main.reprocess_year(year)
            except Exception:
                traceback.print_exc()
                errors.append(f"reprocess:{year}")
            else:
                for url, new_etag in changed:
                    tba_cache.record_success(url, new_etag)
                result["reprocessed"] = True

    next_year = next_sweep_year(year)
    tba_cache.set_sweep_cursor(next_year)
    result["next_year"] = next_year
    tba_cache.persist()  # cursor (global) + any dirty year archive

    if errors:
        result["status"] = "error"
        result["errors"] = errors
    return result
