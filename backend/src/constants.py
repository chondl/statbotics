import os
from typing import List

# GLOBAL

PROD = os.getenv("PROD", "False") == "True"

# 8001 emulates the data server
# The ETL self-trigger (update_curr_year_background, the freshness ping probe)
# HTTP-calls BACKEND_URL to reach the data router. In the original multi-service
# layout the data router was a separate service; on the single-container mirror
# it is the SAME service, so BACKEND_URL must point at the mirror's own backend
# (set the env var to the run.app URL), not upstream api.statbotics.io — else the
# self-trigger hits the wrong host and ingestion never runs.
BACKEND_URL = os.getenv("BACKEND_URL") or (
    "https://api.statbotics.io" if PROD else "http://localhost:8001"
)

# API

AUTH_KEY_BLACKLIST: List[str] = []

# CONFIG

CURR_YEAR = 2026
DISABLE_GCS = False
# Bumped to 2 on 2026-07-30: hist blobs are immutable within an epoch, so the
# 2025 historical blobs still held pre-offseason renders -- team pages for
# past years showed no offseason events even after reprocess-year recomputed
# them correctly. Bumping forces a re-export. Years not yet re-exported into
# the new epoch fall back to the site API, which serves the same data from
# Parquet, and the daily TBA sweep refills them one year per run.
HIST_EPOCH = 2

# There is no relational database. Serving and persistence are
# DuckDB-over-Parquet + GCS blobs (DB retirement, completed 2026-07-27).
# There is deliberately no DISABLE_DB flag: a flag with exactly one legal
# value is not configuration, it is a trap for whoever sets it the other way.

# MISC

EPS = 1e-6
