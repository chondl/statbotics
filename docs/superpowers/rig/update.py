"""Rig partial-update cycle — mirrors the production /v3/data/update_curr_year.

    cd .worktrees/rig/backend
    set -a; source ../../../docs/superpowers/rig/rig.env; set +a
    PYTHONPATH=$PWD .venv/bin/python ../../../docs/superpowers/rig/update.py

partial=True reads existing DB objects, re-fetches TBA with etag caching
(tba_partial=True), replays EPA, and writes changed DB rows + GCS blobs.
This is the cycle Track 1 / Track 2 measure before/after their changes.
"""
import time

import rig_bootstrap  # noqa: F401  registers models + injects real TBA key
from src.data.main import update_curr_year

start = time.time()
update_curr_year(partial=True, tba_partial=True)
print(f"\n=== partial update cycle finished in {time.time() - start:.1f}s ===")
