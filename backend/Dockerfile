# Staging Cloud Run image for the statbotics backend (single service, all routes).
# Prod uses App Engine (deploy/*.yaml) and ignores this file; it exists for the
# staging deploy only.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# libpq for psycopg2 (requirements pins source psycopg2==2.9.10); build tools for
# any wheels that need compiling, then removed to keep the image small.
RUN apt-get update \
    && apt-get install -y --no-install-recommends gcc libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY . .

# Cloud Run injects $PORT (default 8080). Single gunicorn/uvicorn worker mirrors
# prod's data service (structurally single-writer); -t 1200 covers long ETL runs.
ENV PORT=8080
CMD exec gunicorn -w 1 -t 1200 -k uvicorn.workers.UvicornWorker \
    --bind :$PORT main:app
