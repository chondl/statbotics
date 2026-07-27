"""Rig partial-update cycle — mirrors the production /v3/data/update_curr_year.

    cd .worktrees/rig/backend
    set -a; source ../../../docs/superpowers/rig/rig.env; set +a
    PYTHONPATH=$PWD .venv/bin/python ../../../docs/superpowers/rig/update.py

partial=True loads prior pipeline state from the GCS state snapshot, re-fetches
TBA with etag caching (tba_partial=True), replays EPA, and republishes changed
Parquet tables, the snapshot, and site blobs. There is no database.

Requires a seeded rig: db-less partial cycles refuse to run when the snapshot
is unreadable, because seeding from empty objects would publish near-empty
artifacts over good ones. Run seed.py first.
"""
import time

import rig_bootstrap  # noqa: F401  registers models + injects real TBA key
from src.data.main import update_curr_year

start = time.time()
update_curr_year(partial=True, tba_partial=True)
print(f"\n=== partial update cycle finished in {time.time() - start:.1f}s ===")
