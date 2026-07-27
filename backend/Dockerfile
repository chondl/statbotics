# Cloud Run image for the statbotics backend (single service, all routes).
# This is what the mirror runs. The leftover App Engine YAML in deploy/ belongs
# to the upstream split-service layout and is unused on this branch.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# DB retirement Phase 4: there is no relational database, so there is no
# psycopg2 pin to rewrite here any more (the build used to swap the source
# psycopg2 pin for psycopg2-binary to avoid needing a libpq/compile toolchain).
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Cloud Run injects $PORT (default 8080). Single gunicorn/uvicorn worker mirrors
# prod's data service (structurally single-writer); -t 1200 covers long ETL runs.
ENV PORT=8080
CMD exec gunicorn -w 1 -t 1200 -k uvicorn.workers.UvicornWorker \
    --bind :$PORT main:app
