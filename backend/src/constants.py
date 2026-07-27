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
HIST_EPOCH = 1

# DB retirement Phase 4 (design 2026-07-20 §2): there is no relational
# database. Serving and persistence are DuckDB-over-Parquet + GCS blobs, and
# every DB read/write path has been deleted. DISABLE_DB is kept only as an
# accepted-but-vestigial env var so the deploy tooling that still sets
# DISABLE_DB=True (docs/superpowers/rig/deploy/{deploy.sh,Makefile}) keeps
# working unchanged; nothing branches on it any more.
DISABLE_DB = os.getenv("DISABLE_DB", "True") == "True"

# MISC

EPS = 1e-6
