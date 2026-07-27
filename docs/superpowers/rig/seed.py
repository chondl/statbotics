"""Rig seed script — full db-less current-year (2026) build against fake-gcs.

Run from the backend dir of a worktree with the rig venv and rig.env sourced:

    cd .worktrees/rig/backend
    set -a; source ../../../docs/superpowers/rig/rig.env; set +a
    PYTHONPATH=$PWD .venv/bin/python ../../../docs/superpowers/rig/seed.py

partial=False, tba_partial=False -> full recompute of the current year from a
fresh TBA fetch. There is no database (DB retirement Phase 4): the run writes
the year's Parquet tables, the state snapshot, and the site blobs into the
fake-gcs bucket, which is also what every later partial cycle reads from.

Run this first on an empty rig — a partial cycle refuses to run without a
readable snapshot (see the db-less invariant in src/data/main.py). Prior years
are left empty, so absolute EPA seeds differ from prod; that is fine for
gate/diff/publish mechanics, not for asserting absolute EPA values.
"""
import time

import rig_bootstrap  # noqa: F401  registers models + injects real TBA key
from src.data.main import update_curr_year

start = time.time()
update_curr_year(partial=False, tba_partial=False)
print(f"\n=== full curr-year build finished in {time.time() - start:.1f}s ===")
