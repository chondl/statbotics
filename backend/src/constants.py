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

CURR_YEAR = 2027
DISABLE_GCS = False
# The GLOBAL hist epoch, stamped into the manifest's hist_epoch. A past year
# resolves under its year epoch, max(manifest hist_epochs[year] ?? 0,
# hist_epoch) (src/google/year_path.py). A data change to a past year no
# longer needs a bump: year_epoch.publish_past_year moves just that year to a
# new year epoch. Bump this only for a renderer change across all years, and
# then above max(hist_epochs), or the years already advanced keep their older
# render (/info HIST_EPOCH_SHADOWED). Years not yet re-exported into a new
# global epoch fall back to the site API, which serves the same data from
# Parquet; the daily TBA sweep does NOT refill them (it reprocesses a year
# only on a TBA 200).
# Bumped to 2 on 2026-07-30: the 2025 hist blobs still held pre-offseason
# renders, then immutable within an epoch.
HIST_EPOCH = 2

# There is no relational database. Serving and persistence are
# DuckDB-over-Parquet + GCS blobs (DB retirement, completed 2026-07-27).
# There is deliberately no DISABLE_DB flag: a flag with exactly one legal
# value is not configuration, it is a trap for whoever sets it the other way.

# MISC

EPS = 1e-6
