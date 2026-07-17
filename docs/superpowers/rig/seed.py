"""Rig seed script — full current-year (2026) build against local CockroachDB + fake-gcs.

Run from the backend dir of a worktree with the rig venv and rig.env sourced:

    cd .worktrees/rig/backend
    set -a; source ../../../docs/superpowers/rig/rig.env; set +a
    .venv/bin/python ../../../docs/superpowers/rig/seed.py

partial=False, tba_partial=False -> full recompute of the current year from a
fresh TBA fetch, writes DB rows and GCS blobs. Prior years are left empty (EPA
seeds differ from prod; fine for gate/diff/publish mechanics per spec §4).
"""
import time

import rig_bootstrap  # noqa: F401  registers models + injects real TBA key
from src.db.main import Base, engine
from src.data.main import update_curr_year

# Ensure schema exists (idempotent).
Base.metadata.create_all(engine)

start = time.time()
update_curr_year(partial=False, tba_partial=False)
print(f"\n=== full curr-year build finished in {time.time() - start:.1f}s ===")
