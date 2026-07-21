"""Capture a production correctness baseline for the perf-work verification.

Saves canonicalized (parsed, sorted) JSON so later comparisons are semantic,
not byte-level (orjson will change bytes). Usage: python3 capture_baseline.py OUTDIR
"""

import gzip
import json
import sys
import urllib.request
import zlib

API = "https://api-statbotics.iterativerefinement.com"
BUCKET = "https://storage.googleapis.com/statbotics-staging-site"

API_PATHS = [
    "/v3/team/254", "/v3/team/1678", "/v3/team/2056", "/v3/team/148",
    "/v3/year/2024", "/v3/year/2025", "/v3/year/2016", "/v3/year/2005",
    "/v3/site/team_years/2024", "/v3/site/team_years/2025",
    "/v3/site/team_years/2026",
    "/v3/team_years?year=2025&limit=50&metric=epa",
    "/v3/team_years?year=2016&limit=50&metric=epa",
    "/v3/events?year=2025&limit=500",
    "/v3/matches?event=2025casj",
    "/v3/matches?event=2016nytr",
    "/v3/team_matches?team=254&year=2025",
]

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "statbotics-baseline"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read(), r.headers

def main(outdir):
    import os
    os.makedirs(outdir, exist_ok=True)
    index = {}

    # manifest first: the content-hash fingerprint of every published blob
    raw, _ = fetch(f"{BUCKET}/manifest.json")
    try:
        manifest = json.loads(raw)
    except Exception:
        manifest = json.loads(gzip.decompress(raw))
    with open(f"{outdir}/manifest.json", "w") as f:
        json.dump(manifest, f, sort_keys=True, indent=0)
    blobs = manifest.get("blobs", manifest)
    index["manifest_entries"] = len(blobs) if hasattr(blobs, "__len__") else "?"

    for p in API_PATHS:
        name = "api" + p.replace("/", "_").replace("?", "_").replace("&", "_").replace("=", "-") + ".json"
        try:
            raw, _ = fetch(API + p)
            data = json.loads(raw)
            with open(f"{outdir}/{name}", "w") as f:
                json.dump(data, f, sort_keys=True, indent=0)
            index[p] = "ok"
        except Exception as e:
            index[p] = f"ERROR {e}"

    # decompressed site blobs (semantic content) — resolve via manifest when versioned
    blob_paths = [
        "teams/all", "team_years/2024", "team_years/2025", "team_years/2026",
        "events/2025", "events/2026", "team_to_events",
        "team/254", "team/1678",
        "event/2025casj", "event/2016nytr",
    ]
    def resolve(logical):
        if isinstance(blobs, dict) and logical in blobs:
            return f"{BUCKET}/{blobs[logical]}"
        return f"{BUCKET}/{logical}"
    for lp in blob_paths:
        name = "blob_" + lp.replace("/", "_") + ".json"
        try:
            raw, _ = fetch(resolve(lp))
            data = json.loads(zlib.decompress(raw))
            with open(f"{outdir}/{name}", "w") as f:
                json.dump(data, f, sort_keys=True, indent=0)
            index[lp] = "ok"
        except Exception as e:
            index[lp] = f"ERROR {e}"

    with open(f"{outdir}/INDEX.json", "w") as f:
        json.dump(index, f, sort_keys=True, indent=2)
    errs = [k for k, v in index.items() if str(v).startswith("ERROR")]
    print(f"captured {len(index)} items, {len(errs)} errors")
    for k in errs:
        print(" ", k, index[k])

if __name__ == "__main__":
    main(sys.argv[1])
