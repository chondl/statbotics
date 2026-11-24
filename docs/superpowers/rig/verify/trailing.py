"""Verify one trailing-year (or past-year) event end to end on the mirror.

Read-only: GETs the manifest and site blobs from the blob domain and the
event + matches from the public API. Never calls a /v3/data/* or
/v3/site/update_* endpoint.

Prints the manifest's trailing state (``trailing_year``, ``hist_epochs``, the
count of the year's non-Parquet year keys), then resolves ``events/Y`` and
``event/EVENT`` exactly as the frontend does (frontend/src/api/storage.tsx):

- a ``manifest.blobs`` entry wins; otherwise
  ``hist/{max(hist_epochs[year] ?? 0, hist_epoch)}/{path}``;
- a bucket miss (non-200) falls back to the site API (``/v3/site/{path}``);
- ``events/Y`` reads the bucket only for a live year (``CURR_YEAR`` or
  ``manifest.trailing_year``); for any other year the frontend calls the site
  API directly.

Exits non-zero if the resolved event page and the API disagree (status,
num_teams, match count), if EVENT is missing from events/Y, or if an
``--expect-*`` value differs. Run through ``make verify-trailing``.

The year-from-path rule and the hist-epoch rule are loaded from
backend/src/google/year_path.py by file path, so there is no third copy.
Stdlib only.
"""

import argparse
import gzip
import importlib.util
import json
import re
import sys
import time
import urllib.error
import urllib.request
import zlib
from pathlib import Path
from typing import Any, Optional, Tuple

REPO = Path(__file__).resolve().parents[4]
YEAR_PATH = REPO / "backend" / "src" / "google" / "year_path.py"
CONSTANTS = REPO / "backend" / "src" / "constants.py"
USER_AGENT = "statbotics-verify-trailing"


def _load_year_path() -> Any:
    spec = importlib.util.spec_from_file_location("year_path", YEAR_PATH)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load {YEAR_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


year_path = _load_year_path()


def _curr_year() -> int:
    match = re.search(r"^CURR_YEAR = (\d{4})", CONSTANTS.read_text(), re.M)
    if not match:
        raise SystemExit(f"CURR_YEAR not found in {CONSTANTS}")
    return int(match.group(1))


def _get(url: str) -> Tuple[int, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            return res.status, res.read()
    except urllib.error.HTTPError as e:
        return e.code, b""


def _gunzip(raw: bytes) -> bytes:
    return gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw


def fetch_manifest(bucket_url: str) -> Optional[dict]:
    # A cache-buster forces a Cloudflare MISS: the edge caches manifest.json
    # for max-age=60.
    status, raw = _get(f"{bucket_url}/manifest.json?cb={int(time.time())}")
    if status != 200:
        return None
    return json.loads(_gunzip(raw))


def resolve_bucket_url(bucket_url: str, path: str, manifest: Optional[dict]) -> str:
    """frontend/src/api/storage.tsx resolveBucketUrl."""
    if manifest:
        versioned = (manifest.get("blobs") or {}).get(path)
        if versioned:
            return f"{bucket_url}/{versioned}"
        if manifest.get("hist_epoch") is not None:
            epoch = year_path.effective_hist_epoch(manifest, year_path.year_from_path(path))
            return f"{bucket_url}/hist/{epoch}/{path}"
    return f"{bucket_url}/{path}"


def fetch_page(
    bucket_url: str, api_url: str, path: str, manifest: Optional[dict], bucket: bool
) -> Tuple[Optional[Any], str]:
    """(payload, source) as frontend/src/api/storage.tsx fetchAndStore reads
    it: the bucket first when ``bucket``, then the site API."""
    if bucket:
        url = resolve_bucket_url(bucket_url, path, manifest)
        status, raw = _get(url)
        if status == 200:
            return json.loads(zlib.decompress(raw)), url
        print(f"  bucket {url} -> HTTP {status}; falling back to the site API")
    url = f"{api_url}/v3/site/{path}"
    status, raw = _get(url)
    if status == 200:
        return json.loads(raw), url
    return None, f"{url} (HTTP {status})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bucket-url", required=True)
    parser.add_argument("--api-url", required=True)
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--expect-teams", type=int)
    parser.add_argument("--expect-matches", type=int)
    args = parser.parse_args()
    bucket_url = args.bucket_url.rstrip("/")
    api_url = args.api_url.rstrip("/")
    year, event = args.year, args.event
    failures = []

    manifest = fetch_manifest(bucket_url)
    if manifest is None:
        print("manifest.json: unreadable")
        return 1
    blobs = manifest.get("blobs") or {}
    year_keys = [
        k
        for k in blobs
        if year_path.year_from_path(k) == year and not k.startswith("parquet/")
    ]
    trailing_year = manifest.get("trailing_year")
    curr_year = _curr_year()
    live = year == curr_year or year == trailing_year
    print("== manifest ==")
    print(f"  cycle                   {manifest.get('cycle')}")
    print(f"  trailing_year           {json.dumps(trailing_year)}")
    print(f"  hist_epoch              {manifest.get('hist_epoch')}")
    print(f"  hist_epochs             {json.dumps(manifest.get('hist_epochs') or {})}")
    print(f"  {'year epoch of ' + str(year):<24}{year_path.effective_hist_epoch(manifest, year)}")
    print(f"  {'non-Parquet year keys':<24}{len(year_keys)}")
    print(f"  {'live year':<24}{live} (CURR_YEAR {curr_year})")

    print(f"== events/{year} ==")
    events_page, source = fetch_page(bucket_url, api_url, f"events/{year}", manifest, live)
    print(f"  source                  {source}")
    if events_page is None:
        failures.append(f"events/{year} unreadable")
        listed = False
    else:
        listed = any(e.get("key") == event for e in events_page.get("events", []))
        print(f"  events                  {len(events_page.get('events', []))}")
    print(f"  {event + ' listed':<24}{listed}")
    if not listed:
        failures.append(f"{event} missing from events/{year}")

    print(f"== event/{event} (page) ==")
    page, source = fetch_page(bucket_url, api_url, f"event/{event}", manifest, True)
    print(f"  source                  {source}")
    page_vals: Optional[Tuple[Any, Any, int]] = None
    if page is None:
        failures.append(f"event/{event} unreadable")
    else:
        ev = page.get("event") or {}
        page_vals = (ev.get("status"), ev.get("num_teams"), len(page.get("matches") or []))
        print(f"  status                  {page_vals[0]}")
        print(f"  num_teams               {page_vals[1]}")
        print(f"  matches                 {page_vals[2]}")

    print(f"== API {event} ==")
    status, raw = _get(f"{api_url}/v3/event/{event}")
    api_event = json.loads(raw) if status == 200 else {}
    status_m, raw_m = _get(f"{api_url}/v3/matches?event={event}")
    api_matches = json.loads(raw_m) if status_m == 200 else None
    api_vals = (
        api_event.get("status"),
        api_event.get("num_teams"),
        len(api_matches) if isinstance(api_matches, list) else None,
    )
    print(f"  /v3/event               HTTP {status}")
    print(f"  status                  {api_vals[0]}")
    print(f"  num_teams               {api_vals[1]}")
    print(f"  /v3/matches             HTTP {status_m}, count {api_vals[2]}")

    if page_vals is not None and page_vals != api_vals:
        failures.append(
            f"page (status, num_teams, matches) {page_vals} != API {api_vals}"
        )
    for name, expected, index in (
        ("num_teams", args.expect_teams, 1),
        ("matches", args.expect_matches, 2),
    ):
        if expected is None:
            continue
        for label, vals in (("page", page_vals), ("API", api_vals)):
            got = vals[index] if vals is not None else None
            if got != expected:
                failures.append(f"{label} {name} {got} != expected {expected}")

    print("== result ==")
    if failures:
        for f in failures:
            print(f"  FAIL {f}")
        return 1
    print("  OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
