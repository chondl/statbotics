#!/usr/bin/env bash
#
# Reproducible staging deploy for statbotics on Google Cloud.
#
# Deploys the whole stack: Cloud SQL (Postgres) + GCS bucket + backend Cloud Run
# service + seed Cloud Run job + frontend Cloud Run service + Cloud Scheduler +
# Cloudflare DNS/Worker. Idempotent-ish: safe to re-run; most `create` steps are
# guarded and fall back to `update`.
#
# Every account-specific value is a variable (see CONFIG). No secrets are baked in
# — the DB password is generated (or read from the secret file) and the TBA key is
# read from a file; both are written to Secret Manager and to a local chmod-600
# secret file, never echoed.
#
# Prereqs on the operator machine:
#   - gcloud authenticated (gcloud auth login) with rights to the target project
#   - a billing account linked to the project
#   - curl, python3, git
#   - a TBA API key file (format: X-TBA-Auth-Key=<value>)
#   - a Cloudflare API token + account id + a zone you control (for DNS)
#
# Usage:
#   Edit the CONFIG block (or export the vars), then:
#     ./deploy.sh apis         # enable APIs
#     ./deploy.sh sql          # create Cloud SQL instance + db + user
#     ./deploy.sh bucket       # create public GCS bucket + CORS
#     ./deploy.sh secrets      # create Secret Manager entries + IAM
#     ./deploy.sh images       # build backend + frontend images
#     ./deploy.sh backend      # deploy backend Cloud Run service
#     ./deploy.sh seed         # create + run the schema/seed job
#     ./deploy.sh frontend     # deploy frontend Cloud Run service
#     ./deploy.sh scheduler    # hourly update job
#     ./deploy.sh dns          # Cloudflare DNS records + reverse-proxy worker
#     ./deploy.sh all          # everything, in order
#
set -euo pipefail

# ------------------------------- CONFIG --------------------------------------
PROJECT_ID="${PROJECT_ID:-statbotics-staging}"
REGION="${REGION:-us-central1}"

# Cloud SQL
DB_INSTANCE="${DB_INSTANCE:-statbotics-staging-db}"
DB_TIER="${DB_TIER:-db-f1-micro}"          # smallest shared-core Postgres tier
DB_VERSION="${DB_VERSION:-POSTGRES_15}"
DB_NAME="${DB_NAME:-statbotics3}"
DB_USER="${DB_USER:-statbotics}"
DB_STORAGE_GB="${DB_STORAGE_GB:-10}"

# GCS bucket (globally unique; cannot be site_v1/site_dev_v1)
BUCKET_NAME="${BUCKET_NAME:-statbotics-staging-site}"

# Artifact Registry
AR_REPO="${AR_REPO:-statbotics}"

# Cloud Run
API_SERVICE="${API_SERVICE:-statbotics-api}"
WEB_SERVICE="${WEB_SERVICE:-statbotics-web}"
SEED_JOB="${SEED_JOB:-statbotics-seed}"
API_MEMORY="${API_MEMORY:-8Gi}"            # full-stack cycle in the serving container: EPA replay + cycle-start deepcopy + snapshot(24MB) + Parquet serialization + DuckDB cache. Measured peaks: 2Gi OOM @2055MiB, 4Gi OOM @4166MiB. 8Gi (needs >=2 CPU) clears it.
API_CPU="${API_CPU:-2}"                     # 8Gi memory on Cloud Run requires >=2 vCPU
WEB_MEMORY="${WEB_MEMORY:-1Gi}"
WEB_CPU="${WEB_CPU:-1}"
SEED_MEMORY="${SEED_MEMORY:-4Gi}"
MAX_INSTANCES="${MAX_INSTANCES:-2}"

# Domains (must be inside the Cloudflare zone below)
FRONTEND_DOMAIN="${FRONTEND_DOMAIN:-statbotics.iterativerefinement.com}"
API_DOMAIN="${API_DOMAIN:-api-statbotics.iterativerefinement.com}"
# Cloudflare-proxied hostname for the public blob bucket (edge-cached, so the
# frontend reads blobs through the CDN instead of straight from GCS).
BLOB_DOMAIN="${BLOB_DOMAIN:-blobs-statbotics.iterativerefinement.com}"

# Source tree (the merged `staging` branch checkout)
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
BACKEND_DIR="${BACKEND_DIR:-$REPO_DIR/backend}"
FRONTEND_DIR="${FRONTEND_DIR:-$REPO_DIR/frontend}"

# Secret inputs (never printed)
TBA_KEY_FILE="${TBA_KEY_FILE:-$HOME/thebluealliance_api_key.txt}"   # X-TBA-Auth-Key=<value>
STAGING_SECRET_FILE="${STAGING_SECRET_FILE:-$HOME/statbotics_staging_secret.txt}"

# Cloudflare (token + account read from a KEY=VALUE file; never printed)
CF_SECRET_FILE="${CF_SECRET_FILE:-$HOME/iterativerefinement_secret.txt}"
CF_ZONE_NAME="${CF_ZONE_NAME:-iterativerefinement.com}"
WORKER_NAME="${WORKER_NAME:-statbotics-proxy}"
BLOB_WORKER_NAME="${BLOB_WORKER_NAME:-statbotics-blob-proxy}"

IMAGE_API="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/$API_SERVICE:latest"
IMAGE_WEB="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/$WEB_SERVICE:latest"
CONN_NAME="$PROJECT_ID:$REGION:$DB_INSTANCE"
# psycopg2 unix-socket URL; password comes from PGPASSWORD (secret), not the URL.
DATABASE_URL="postgresql+psycopg2://$DB_USER@/$DB_NAME?host=/cloudsql/$CONN_NAME"

gc() { gcloud --project="$PROJECT_ID" "$@"; }

# ------------------------------- STEPS ---------------------------------------

step_apis() {
  gc services enable \
    sqladmin.googleapis.com run.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com secretmanager.googleapis.com \
    cloudscheduler.googleapis.com storage.googleapis.com compute.googleapis.com
}

step_sql() {
  if ! gc sql instances describe "$DB_INSTANCE" >/dev/null 2>&1; then
    gc sql instances create "$DB_INSTANCE" \
      --database-version="$DB_VERSION" --tier="$DB_TIER" --region="$REGION" \
      --availability-type=ZONAL --storage-size="$DB_STORAGE_GB" \
      --storage-type=HDD --no-backup --quiet
  fi
  # Generate/read the DB password and persist it to the local secret file.
  if [ -f "$STAGING_SECRET_FILE" ] && grep -q '^CLOUDSQL_PASSWORD=' "$STAGING_SECRET_FILE"; then
    DB_PASSWORD="$(grep '^CLOUDSQL_PASSWORD=' "$STAGING_SECRET_FILE" | cut -d= -f2-)"
  else
    DB_PASSWORD="$(python3 -c 'import secrets,string; print("".join(secrets.choice(string.ascii_letters+string.digits) for _ in range(28)))')"
    umask 077
    {
      echo "# Statbotics staging secrets — untracked, chmod 600."
      echo "GCP_PROJECT=$PROJECT_ID"
      echo "GCP_REGION=$REGION"
      echo "CLOUDSQL_INSTANCE=$DB_INSTANCE"
      echo "CLOUDSQL_DB=$DB_NAME"
      echo "CLOUDSQL_USER=$DB_USER"
      echo "CLOUDSQL_PASSWORD=$DB_PASSWORD"
      echo "GCS_BUCKET=$BUCKET_NAME"
    } > "$STAGING_SECRET_FILE"
    chmod 600 "$STAGING_SECRET_FILE"
  fi
  gc sql databases create "$DB_NAME" --instance="$DB_INSTANCE" 2>/dev/null || true
  gc sql users create "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASSWORD" 2>/dev/null \
    || gc sql users set-password "$DB_USER" --instance="$DB_INSTANCE" --password="$DB_PASSWORD"
}

step_bucket() {
  gc storage buckets create "gs://$BUCKET_NAME" \
    --location="$REGION" --uniform-bucket-level-access 2>/dev/null || true
  gc storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
    --member=allUsers --role=roles/storage.objectViewer
  local cors; cors="$(mktemp)"
  cat > "$cors" <<EOF
[{"origin":["https://$FRONTEND_DOMAIN","http://localhost:3000"],
  "responseHeader":["Content-Type","Cache-Control"],
  "method":["GET","HEAD","OPTIONS"],"maxAgeSeconds":3600}]
EOF
  gc storage buckets update "gs://$BUCKET_NAME" --cors-file="$cors"
  rm -f "$cors"
}

step_secrets() {
  local tba; tba="$(grep -o '=.*' "$TBA_KEY_FILE" | head -1 | cut -c2-)"
  printf '%s' "$tba" | gc secrets create tba-auth-key --data-file=- --replication-policy=automatic 2>/dev/null \
    || printf '%s' "$tba" | gc secrets versions add tba-auth-key --data-file=-
  local dbpw; dbpw="$(grep '^CLOUDSQL_PASSWORD=' "$STAGING_SECRET_FILE" | cut -d= -f2-)"
  printf '%s' "$dbpw" | gc secrets create db-password --data-file=- --replication-policy=automatic 2>/dev/null \
    || printf '%s' "$dbpw" | gc secrets versions add db-password --data-file=-

  gc artifacts repositories create "$AR_REPO" --repository-format=docker \
    --location="$REGION" 2>/dev/null || true

  local num sa
  num="$(gc projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  sa="${num}-compute@developer.gserviceaccount.com"
  for s in tba-auth-key db-password; do
    gc secrets add-iam-policy-binding "$s" \
      --member="serviceAccount:$sa" --role=roles/secretmanager.secretAccessor
  done
  gc projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$sa" --role=roles/cloudsql.client
  gc storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
    --member="serviceAccount:$sa" --role=roles/storage.objectAdmin
}

step_images() {
  ( cd "$BACKEND_DIR" && gc builds submit --tag "$IMAGE_API" --timeout=1200 . )
  local cfg; cfg="$(mktemp).yaml"
  cat > "$cfg" <<EOF
steps:
  - name: gcr.io/cloud-builders/docker
    args: [build,
      --build-arg=BACKEND_URL=https://$API_DOMAIN/v3/site,
      --build-arg=BUCKET_URL=https://$BLOB_DOMAIN,
      --build-arg=PROD=True, -t, $IMAGE_WEB, .]
images: [$IMAGE_WEB]
options: {machineType: E2_HIGHCPU_8}
EOF
  ( cd "$FRONTEND_DIR" && gc builds submit --config="$cfg" --timeout=1800 . )
  rm -f "$cfg"
}

# Full-stack mode: serve /v3 from DuckDB-over-Parquet (API_BACKEND=duckdb). The DB
# stays connected (no DISABLE_DB) — it is still written each cycle and is the
# fallback. DISABLE_DB=True is a demonstrated db-less toggle, NOT the deployed
# default: setting it makes the pipeline skip DB writes and forces DuckDB serving
# from Parquet + the state snapshot (requires a full historical Parquet backfill
# first, or EPA seeds regress to the rookie mean). To flip staging to a db-less
# demo, redeploy with `--update-env-vars=DISABLE_DB=True`; revert by removing it.
API_BACKEND="${API_BACKEND:-duckdb}"

step_backend() {
  gc run deploy "$API_SERVICE" \
    --image="$IMAGE_API" --region="$REGION" --platform=managed \
    --allow-unauthenticated --add-cloudsql-instances="$CONN_NAME" \
    --set-env-vars="^|^PROD=True|GCS_BUCKET=$BUCKET_NAME|DATABASE_URL=$DATABASE_URL|API_BACKEND=$API_BACKEND" \
    --set-secrets="TBA_AUTH_KEY=tba-auth-key:latest,PGPASSWORD=db-password:latest" \
    --min-instances=0 --max-instances="$MAX_INSTANCES" \
    --cpu="$API_CPU" --memory="$API_MEMORY" --timeout=3600
}

step_seed() {
  # create_all needs the model classes imported first (import src.db.models);
  # update_curr_year(partial=False) writes GCS blobs BEFORE post_process inserts
  # Team rows, so a trailing partial cycle re-renders teams/all with teams present.
  local seed='import src.db.models; from src.data.main import update_curr_year, refresh_teams; from src.db.main import Base, engine; Base.metadata.create_all(engine); update_curr_year(partial=False, tba_partial=False); refresh_teams(); update_curr_year(partial=True, tba_partial=True); print("SEED_COMPLETE")'
  gc run jobs delete "$SEED_JOB" --region="$REGION" --quiet 2>/dev/null || true
  gc run jobs create "$SEED_JOB" \
    --image="$IMAGE_API" --region="$REGION" \
    --set-cloudsql-instances="$CONN_NAME" \
    --set-env-vars="^|^PROD=True|GCS_BUCKET=$BUCKET_NAME|DATABASE_URL=$DATABASE_URL" \
    --set-secrets="TBA_AUTH_KEY=tba-auth-key:latest,PGPASSWORD=db-password:latest" \
    --command=python --args="^@@@^-c@@@$seed" \
    --cpu=2 --memory="$SEED_MEMORY" --task-timeout=1800 --max-retries=0
  gc run jobs execute "$SEED_JOB" --region="$REGION" --wait
}

step_frontend() {
  gc run deploy "$WEB_SERVICE" \
    --image="$IMAGE_WEB" --region="$REGION" --platform=managed \
    --allow-unauthenticated \
    --min-instances=0 --max-instances="$MAX_INSTANCES" \
    --cpu="$WEB_CPU" --memory="$WEB_MEMORY" --timeout=300
}

step_scheduler() {
  local api_url
  api_url="$(gc run services describe "$API_SERVICE" --region="$REGION" --format='value(status.url)')"
  gc scheduler jobs create http statbotics-update \
    --location="$REGION" --schedule="0 * * * *" \
    --uri="$api_url/v3/site/update_curr_year" --http-method=GET \
    --attempt-deadline=1800s \
    --description="Hourly offseason update_curr_year (etag precheck + backgrounded partial)" \
    2>/dev/null || true
}

step_dns() {
  local token account zone api_host web_host
  token="$(grep '^CLOUDFLARE_API_TOKEN=' "$CF_SECRET_FILE" | cut -d= -f2-)"
  account="$(grep '^CLOUDFLARE_ACCOUNT_ID=' "$CF_SECRET_FILE" | cut -d= -f2-)"
  zone="$(curl -s -H "Authorization: Bearer $token" \
    "https://api.cloudflare.com/client/v4/zones?name=$CF_ZONE_NAME" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"][0]["id"])')"
  api_host="$(gc run services describe "$API_SERVICE" --region="$REGION" --format='value(status.url)' | sed 's#https://##')"
  web_host="$(gc run services describe "$WEB_SERVICE" --region="$REGION" --format='value(status.url)' | sed 's#https://##')"

  # Proxied (orange-cloud) placeholder AAAA records so the Worker route intercepts.
  for name in "${FRONTEND_DOMAIN%%.*}" "${API_DOMAIN%%.*}"; do
    curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$zone/dns_records" \
      -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
      --data "{\"type\":\"AAAA\",\"name\":\"$name\",\"content\":\"100::\",\"proxied\":true}" >/dev/null
  done

  # Reverse-proxy Worker: rewrite Host to the run.app origin (Cloud Run routes by host).
  local worker; worker="$(mktemp).js"
  cat > "$worker" <<EOF
const MAP = {
  "$FRONTEND_DOMAIN": "$web_host",
  "$API_DOMAIN": "$api_host",
};
addEventListener("fetch", (e) => e.respondWith(handle(e.request)));
async function handle(request) {
  const url = new URL(request.url);
  const origin = MAP[url.hostname];
  if (!origin) return new Response("Not found", { status: 404 });
  url.hostname = origin; url.protocol = "https:"; url.port = "";
  return fetch(url.toString(), request);
}
EOF
  curl -s -X PUT "https://api.cloudflare.com/client/v4/accounts/$account/workers/scripts/$WORKER_NAME" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/javascript" \
    --data-binary @"$worker" >/dev/null
  rm -f "$worker"
  for name in "$FRONTEND_DOMAIN" "$API_DOMAIN"; do
    curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$zone/workers/routes" \
      -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
      --data "{\"pattern\":\"$name/*\",\"script\":\"$WORKER_NAME\"}" >/dev/null
  done

  # Blob proxy: a second Worker that reverse-proxies the public GCS bucket behind
  # $BLOB_DOMAIN so blob reads are edge-cached. It forwards the path to
  # storage.googleapis.com/$BUCKET_NAME/... and lets Cloudflare cache per the
  # origin Cache-Control (immutable v2/{hash} blobs cache ~1y at the edge). The
  # short-lived manifest.json is ALSO edge-cached (cacheEverything honors its
  # origin max-age=60), but the Worker rewrites the client-facing Cache-Control
  # back to max-age=60 so the zone's Browser Cache TTL floor cannot stretch it —
  # the Worker's own response header wins over the floor, verified live. This
  # lets co-located event visitors share one edge-cached manifest per PoP
  # instead of each paying an origin RTT.
  curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$zone/dns_records" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    --data "{\"type\":\"AAAA\",\"name\":\"${BLOB_DOMAIN%%.*}\",\"content\":\"100::\",\"proxied\":true}" >/dev/null
  local blobworker; blobworker="$(mktemp).js"
  cat > "$blobworker" <<EOF
const BUCKET = "$BUCKET_NAME";
const ORIGIN = "https://storage.googleapis.com";
addEventListener("fetch", (event) => { event.respondWith(handle(event.request)); });
async function handle(request) {
  const url = new URL(request.url);
  const originUrl = ORIGIN + "/" + BUCKET + url.pathname + url.search;
  const originReq = new Request(originUrl, request);
  const isManifest = url.pathname === "/manifest.json";
  const res = await fetch(originReq, { cf: { cacheEverything: true } });
  if (isManifest) {
    const out = new Response(res.body, res);
    out.headers.set("Cache-Control", "public, max-age=60");
    return out;
  }
  return res;
}
EOF
  curl -s -X PUT "https://api.cloudflare.com/client/v4/accounts/$account/workers/scripts/$BLOB_WORKER_NAME" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/javascript" \
    --data-binary @"$blobworker" >/dev/null
  rm -f "$blobworker"
  curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$zone/workers/routes" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    --data "{\"pattern\":\"$BLOB_DOMAIN/*\",\"script\":\"$BLOB_WORKER_NAME\"}" >/dev/null
}

case "${1:-all}" in
  apis) step_apis ;;
  sql) step_sql ;;
  bucket) step_bucket ;;
  secrets) step_secrets ;;
  images) step_images ;;
  backend) step_backend ;;
  seed) step_seed ;;
  frontend) step_frontend ;;
  scheduler) step_scheduler ;;
  dns) step_dns ;;
  all)
    step_apis; step_sql; step_bucket; step_secrets; step_images
    step_backend; step_seed; step_frontend; step_scheduler; step_dns ;;
  *) echo "unknown step: $1" >&2; exit 1 ;;
esac
echo "OK: ${1:-all}"
