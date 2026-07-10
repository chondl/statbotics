# Staging Cloud Run image for the statbotics backend (single service, all routes).
# Prod uses App Engine (deploy/*.yaml) and ignores this file; it exists for the
# staging deploy only.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# requirements.txt pins source psycopg2==2.9.10, which needs a full libpq/compile
# toolchain. psycopg2-binary is the identical module with a bundled libpq (the
# same swap the local rig uses). Keep the tracked requirements.txt pristine by
# rewriting the pin at build time rather than editing the file.
COPY requirements.txt ./
RUN sed 's/^psycopg2==/psycopg2-binary==/' requirements.txt > requirements.staging.txt \
    && pip install -r requirements.staging.txt

COPY . .

# Cloud Run injects $PORT (default 8080). Single gunicorn/uvicorn worker mirrors
# prod's data service (structurally single-writer); -t 1200 covers long ETL runs.
ENV PORT=8080
CMD exec gunicorn -w 1 -t 1200 -k uvicorn.workers.UvicornWorker \
    --bind :$PORT main:app
