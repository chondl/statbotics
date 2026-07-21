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

# DB

CRDB_USER = os.getenv("CRDB_USER", "")
CRDB_PWD = os.getenv("CRDB_PWD", "")
CRDB_HOST = os.getenv("CRDB_HOST", "")

# DATABASE_URL, if set, overrides the CockroachDB connection string entirely.
# Used to run the backend against plain PostgreSQL (e.g. Cloud SQL staging),
# e.g. "postgresql+psycopg2://user:pwd@host:5432/statbotics3". The transaction
# helper (src/db/transaction.py) selects retry behavior from the engine dialect.
CONN_STR = os.getenv("DATABASE_URL") or (
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
DISABLE_DB = os.getenv("DISABLE_DB", "False") == "True"
HIST_EPOCH = 1

# MISC

EPS = 1e-6
