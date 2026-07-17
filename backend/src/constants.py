import os
from typing import List

# GLOBAL

PROD = os.getenv("PROD", "False") == "True"

# 8001 emulates the data server
# The ETL self-trigger (update_curr_year_background and the freshness-ping probe)
# HTTP-calls BACKEND_URL to reach the data router. When the data router runs as a
# separate service the default is right, but a single-container deploy (e.g. a
# staging mirror serving all routers from one service) must point BACKEND_URL at
# its OWN backend, or the self-trigger hits the wrong host and ingestion never
# runs. Read it from the environment, falling back to the original default.
BACKEND_URL = os.getenv("BACKEND_URL") or (
    "https://api.statbotics.io" if PROD else "http://localhost:8001"
)

# DB

CRDB_USER = os.getenv("CRDB_USER", "")
CRDB_PWD = os.getenv("CRDB_PWD", "")
CRDB_HOST = os.getenv("CRDB_HOST", "")

CONN_STR = (
    (
        "cockroachdb://"
        + CRDB_USER
        + ":"
        + CRDB_PWD
        + "@"
        + CRDB_HOST
        + "/statbotics3?sslmode=verify-full&sslrootcert=root.crt"
    )
    if PROD
    else "cockroachdb://root@localhost:26257/statbotics3?sslmode=disable"
)

# API

AUTH_KEY_BLACKLIST: List[str] = []

# CONFIG

CURR_YEAR = 2026
DISABLE_GCS = False

# MISC

EPS = 1e-6
