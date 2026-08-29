#!/usr/bin/env bash
#
# Reproducible staging deploy for statbotics on Google Cloud.
#
# Deploys the whole stack: GCS bucket + backend Cloud Run service + seed Cloud
# Run job + frontend Cloud Run services + Cloud Scheduler + Cloudflare DNS/Worker.
# Idempotent-ish: safe to re-run; most `create` steps are guarded and fall back
# to `update`.
#
# ONE FRONTEND, ONE BACKEND, ONE LEGACY REDIRECT. Since 2026-08-29 the
# PRIMARY (and only) user-facing domain is statbotics.popcornpenguins.com
# (Cloud Run `statbotics-web-pp`, Cloudflare account "Popcorn Penguins" /
# zone popcornpenguins.com). The former mirror domain on zone
# iterativerefinement.com (Cloudflare account #1) is LEGACY:
#   - statbotics.iterativerefinement.com       -> 301 to the primary frontend
#     (path+query preserved; the Cloud Run `statbotics-web` service that used
#     to serve it is deleted)
#   - api-statbotics.iterativerefinement.com   -> still proxies the backend
#     (kept alive for external API clients that may not follow redirects)
#   - blobs-statbotics.iterativerefinement.com -> still proxies the bucket
#     (kept alive for old open tabs / external blob readers)
# The backend Cloud Run service, GCS bucket, seed job, and schedulers are
# shared; both zones' api-/blobs- hostnames proxy the same origins.
#
# NO DATABASE. DB retirement Phase 4 completed 2026-07-27: the Cloud SQL
# instance, the `db-password` secret, the `cloudsql.client` IAM grant, and the
# `statbotics-seed` DB-mode job were all deleted. Serving and the update cycle
# run entirely on GCS Parquet + DuckDB. The `sql` step is gone; do not add it
# back. A final pg_dump of the retired DB lives at
# gs://statbotics-staging-db-final-export/ (private).
#
# Every account-specific value is a variable (see CONFIG). No secrets are baked
# in and none are ever echoed.
#
# SECRETS ARE ENVIRONMENT VARIABLES — ONLY. The canonical store is the
# operator's macOS Keychain; every secret arrives in the shell as an
# environment variable at start (home-directory secret files are fully
# retired). A missing secret fails fast naming the VARIABLE. Variables:
#   TBA_AUTH_KEY                          (step: secrets)
#   POPCORNPENGUINS_CLOUDFLARE_API_TOKEN  (step: dns-pp — PRIMARY zone popcornpenguins.com)
#   POPCORNPENGUINS_CLOUDFLARE_ACCOUNT_ID (step: dns-pp)
#   CLOUDFLARE_API_TOKEN                  (step: dns-legacy — legacy zone iterativerefinement.com)
#   CLOUDFLARE_ACCOUNT_ID                 (step: dns-legacy)
#
# Prereqs on the operator machine:
#   - gcloud authenticated (gcloud auth login) with rights to the target project
#   - a billing account linked to the project
#   - curl, python3, git
#   - the secret environment variables above (for the steps being run)
#
# Usage:
#   Edit the CONFIG block (or export the vars), then:
#     ./deploy.sh apis         # enable APIs
#     ./deploy.sh bucket       # create public GCS bucket + CORS
#     ./deploy.sh secrets      # create Secret Manager entries + IAM
#     ./deploy.sh images       # build backend + frontend images
#     ./deploy.sh backend      # deploy backend Cloud Run service
#     ./deploy.sh seed         # create + run the schema/seed job
#     ./deploy.sh frontend     # deploy the frontend Cloud Run service
#     ./deploy.sh scheduler    # hourly update job
#     ./deploy.sh dns-pp       # PRIMARY zone popcornpenguins.com: records + proxy workers
#     ./deploy.sh dns-legacy   # legacy zone iterativerefinement.com: frontend 301 + api/blob proxies
#                              #   (REDIRECT_STATUS=302 ./deploy.sh dns-legacy for a temporary redirect)
#     ./deploy.sh all          # everything, in order
#
set -euo pipefail

# ------------------------------- CONFIG --------------------------------------
PROJECT_ID="${PROJECT_ID:-statbotics-staging}"
REGION="${REGION:-us-central1}"

# GCS bucket (globally unique; cannot be site_v1/site_dev_v1)
BUCKET_NAME="${BUCKET_NAME:-statbotics-staging-site}"

# Artifact Registry
AR_REPO="${AR_REPO:-statbotics}"

# Cloud Run. The frontend service keeps its historical `-pp` suffix: it was
# born as the popcornpenguins mirror while `statbotics-web` (deleted
# 2026-08-29) served the old iterativerefinement domain. Renaming a live
# Cloud Run service is not worth the churn.
API_SERVICE="${API_SERVICE:-statbotics-api}"
WEB_SERVICE="${WEB_SERVICE:-statbotics-web-pp}"
SEED_JOB="${SEED_JOB:-statbotics-seed}"
API_MEMORY="${API_MEMORY:-8Gi}"            # full-stack cycle in the serving container: EPA replay + cycle-start deepcopy + snapshot(24MB) + Parquet serialization + DuckDB cache. Measured peaks: 2Gi OOM @2055MiB, 4Gi OOM @4166MiB. 8Gi (needs >=2 CPU) clears it.
API_CPU="${API_CPU:-2}"                     # 8Gi memory on Cloud Run requires >=2 vCPU
WEB_MEMORY="${WEB_MEMORY:-1Gi}"
WEB_CPU="${WEB_CPU:-1}"
SEED_MEMORY="${SEED_MEMORY:-4Gi}"
MAX_INSTANCES="${MAX_INSTANCES:-2}"

# PRIMARY domains (zone popcornpenguins.com, Cloudflare acct "Popcorn Penguins").
FRONTEND_DOMAIN="${FRONTEND_DOMAIN:-statbotics.popcornpenguins.com}"
API_DOMAIN="${API_DOMAIN:-api-statbotics.popcornpenguins.com}"
# Cloudflare-proxied hostname for the public blob bucket (edge-cached, so the
# frontend reads blobs through the CDN instead of straight from GCS).
BLOB_DOMAIN="${BLOB_DOMAIN:-blobs-statbotics.popcornpenguins.com}"

# LEGACY domains (zone iterativerefinement.com, Cloudflare acct #1). The
# frontend hostname 301s to the primary; api-/blobs- still proxy the shared
# backend/bucket for legacy clients. See the header comment.
LEGACY_FRONTEND_DOMAIN="${LEGACY_FRONTEND_DOMAIN:-statbotics.iterativerefinement.com}"
LEGACY_API_DOMAIN="${LEGACY_API_DOMAIN:-api-statbotics.iterativerefinement.com}"
LEGACY_BLOB_DOMAIN="${LEGACY_BLOB_DOMAIN:-blobs-statbotics.iterativerefinement.com}"
# 301 in steady state; export REDIRECT_STATUS=302 to stage a new target
# reversibly before committing browsers to a permanent redirect.
REDIRECT_STATUS="${REDIRECT_STATUS:-301}"

# Source tree (the merged `staging` branch checkout)
REPO_DIR="${REPO_DIR:-$(cd "$(dirname "$0")/../../../.." && pwd)}"
BACKEND_DIR="${BACKEND_DIR:-$REPO_DIR/backend}"
FRONTEND_DIR="${FRONTEND_DIR:-$REPO_DIR/frontend}"

# Cloudflare zones. Worker script names are per-account, so both accounts can
# use the same names.
CF_ZONE_NAME="${CF_ZONE_NAME:-popcornpenguins.com}"
LEGACY_CF_ZONE_NAME="${LEGACY_CF_ZONE_NAME:-iterativerefinement.com}"
WORKER_NAME="${WORKER_NAME:-statbotics-proxy}"
BLOB_WORKER_NAME="${BLOB_WORKER_NAME:-statbotics-blob-proxy}"

IMAGE_API="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/$API_SERVICE:latest"
IMAGE_WEB="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO/$WEB_SERVICE:latest"

gc() { gcloud --project="$PROJECT_ID" "$@"; }

# require_env VAR — print the secret from the environment variable VAR, or
# fail fast naming the VARIABLE. Secrets come exclusively from the operator's
# environment (Keychain-backed); there is no file fallback.
require_env() {
  local var="$1" val
  val="${!var:-}"
  if [ -z "$val" ]; then
    echo "ERROR: \$$var is not set. It is provided by the operator's environment — declare it there, then re-run." >&2
    return 1
  fi
  printf '%s' "$val"
}

# ------------------------------- STEPS ---------------------------------------

step_apis() {
  gc services enable \
    run.googleapis.com cloudbuild.googleapis.com \
    artifactregistry.googleapis.com secretmanager.googleapis.com \
    cloudscheduler.googleapis.com storage.googleapis.com compute.googleapis.com
}

step_bucket() {
  gc storage buckets create "gs://$BUCKET_NAME" \
    --location="$REGION" --uniform-bucket-level-access 2>/dev/null || true
  gc storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
    --member=allUsers --role=roles/storage.objectViewer
  # The legacy origin stays in CORS: tabs opened before the 301 cutover keep
  # fetching blobs with the old Origin until their next full page load.
  local cors; cors="$(mktemp)"
  cat > "$cors" <<EOF
[{"origin":["https://$FRONTEND_DOMAIN","https://$LEGACY_FRONTEND_DOMAIN","http://localhost:3000"],
  "responseHeader":["Content-Type","Cache-Control"],
  "method":["GET","HEAD","OPTIONS"],"maxAgeSeconds":3600}]
EOF
  gc storage buckets update "gs://$BUCKET_NAME" --cors-file="$cors"
  rm -f "$cors"
}

step_secrets() {
  local tba; tba="$(require_env TBA_AUTH_KEY)"
  printf '%s' "$tba" | gc secrets create tba-auth-key --data-file=- --replication-policy=automatic 2>/dev/null \
    || printf '%s' "$tba" | gc secrets versions add tba-auth-key --data-file=-
  gc artifacts repositories create "$AR_REPO" --repository-format=docker \
    --location="$REGION" 2>/dev/null || true

  local num sa
  num="$(gc projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  sa="${num}-compute@developer.gserviceaccount.com"
  gc secrets add-iam-policy-binding tba-auth-key \
    --member="serviceAccount:$sa" --role=roles/secretmanager.secretAccessor
  gc storage buckets add-iam-policy-binding "gs://$BUCKET_NAME" \
    --member="serviceAccount:$sa" --role=roles/storage.objectAdmin
}

# build_web_image IMAGE API_DOMAIN BLOB_DOMAIN — BACKEND_URL/BUCKET_URL are
# inlined into the JS bundle at `next build` time, so the frontend image is
# domain-specific by construction.
build_web_image() {
  local image="$1" api_domain="$2" blob_domain="$3"
  local cfg; cfg="$(mktemp).yaml"
  cat > "$cfg" <<EOF
steps:
  - name: gcr.io/cloud-builders/docker
    args: [build,
      --build-arg=BACKEND_URL=https://$api_domain/v3/site,
      --build-arg=BUCKET_URL=https://$blob_domain,
      --build-arg=PROD=True, -t, $image, .]
images: [$image]
options: {machineType: E2_HIGHCPU_8}
EOF
  ( cd "$FRONTEND_DIR" && gc builds submit --config="$cfg" --timeout=1800 . )
  rm -f "$cfg"
}

step_images() {
  ( cd "$BACKEND_DIR" && gc builds submit --tag "$IMAGE_API" --timeout=1200 . )
  build_web_image "$IMAGE_WEB" "$API_DOMAIN" "$BLOB_DOMAIN"
}

# Serve /v3 from DuckDB-over-Parquet (API_BACKEND=duckdb). There is no
# database and no flag to disable one — that is simply how the backend works
# (DB retirement completed 2026-07-27).
#
# BACKEND_URL is MANDATORY and easy to miss. The ETL self-trigger (the ping
# probe and update_curr_year_background) HTTP-calls it to reach the data
# router, which on this single-container mirror is the SAME service. If it is
# unset, src/constants.py falls back to https://api.statbotics.io — UPSTREAM's
# API — and the mirror's ingestion silently stops running while every health
# check still passes. It was missing from this step until 2026-07-27; the live
# service had it only because it was set by hand.
API_BACKEND="${API_BACKEND:-duckdb}"

step_backend() {
  gc run deploy "$API_SERVICE" \
    --image="$IMAGE_API" --region="$REGION" --platform=managed \
    --allow-unauthenticated \
    --set-env-vars="^|^PROD=True|GCS_BUCKET=$BUCKET_NAME|API_BACKEND=$API_BACKEND|BACKEND_URL=https://$API_DOMAIN" \
    --set-secrets="TBA_AUTH_KEY=tba-auth-key:latest" \
    --min-instances=0 --max-instances="$MAX_INSTANCES" \
    --cpu="$API_CPU" --memory="$API_MEMORY" --timeout=3600
}

step_seed() {
  # Db-less stand-up (DB retirement Phase 4, 2026-07-27). The old DB-mode seed
  # (create_all + a DB-backed cycle) is gone along with the instance.
  #
  # reset_all_years re-ingests 2002..present from TBA and publishes every year's
  # Parquet + hist/ blobs, then the current-year cycle + refresh_teams render the
  # teams/ and team/{num} blobs. That IS the whole datastore now — there is
  # nothing else to seed. The TBA persistent cache makes this ~5-8 min on a warm
  # bucket; from a cold bucket it is bounded by TBA fetch time, hence the 2 h
  # task timeout.
  local seed='import src.db.models; from src.data.main import reset_all_years, refresh_teams, update_curr_year; reset_all_years(); refresh_teams(); update_curr_year(partial=True, tba_partial=True); print("SEED_COMPLETE")'
  gc run jobs delete "$SEED_JOB" --region="$REGION" --quiet 2>/dev/null || true
  gc run jobs create "$SEED_JOB" \
    --image="$IMAGE_API" --region="$REGION" \
    --set-env-vars="^|^PROD=True|GCS_BUCKET=$BUCKET_NAME" \
    --set-secrets="TBA_AUTH_KEY=tba-auth-key:latest" \
    --command=python --args="^@@@^-c@@@$seed" \
    --cpu=2 --memory="$SEED_MEMORY" --task-timeout=7200 --max-retries=0
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
  # Daily TBA historical revalidation sweep (cache design §2.3): one
  # round-robin year per day, serial conditional GETs; reprocesses the year
  # in-request when anything changed. 05:30 UTC offsets it from the hourly
  # update (on the hour) and statbotics-gc (04:30); 1800s is Cloud
  # Scheduler's max attempt-deadline — a longer reprocess continues
  # server-side even if the scheduler stops waiting.
  gc scheduler jobs create http statbotics-tba-sweep \
    --location="$REGION" --schedule="30 5 * * *" \
    --uri="$api_url/v3/data/revalidate_tba" --http-method=GET \
    --attempt-deadline=1800s \
    --description="Daily TBA historical revalidation sweep (one year, serial; reprocess on change)" \
    2>/dev/null || true
  # Daily team-fields refresh (DB retirement Phase 1 item 4): re-syncs team
  # names / rookie years / records from TBA (one uncached teams fetch) and
  # persists — DB writes in DB mode, targeted artifact republish db-less.
  # This job is the operating guarantee that no operator action is ever
  # needed to keep team fields current. 06:30 UTC offsets it from the hourly
  # update (on the hour), statbotics-gc (04:30), and the TBA sweep (05:30).
  gc scheduler jobs create http statbotics-refresh-teams \
    --location="$REGION" --schedule="30 6 * * *" \
    --uri="$api_url/v3/data/refresh_teams" --http-method=GET \
    --attempt-deadline=1800s \
    --description="Daily refresh_teams (TBA team-field re-sync; republish db-less)" \
    2>/dev/null || true
}

# deploy_cf_target TOKEN ACCOUNT ZONE_NAME FRONTEND_DOMAIN API_DOMAIN BLOB_DOMAIN WEB_SERVICE
# The PRIMARY zone's edge config: proxied AAAA records + the reverse-proxy
# Worker (frontend + api) + the blob-proxy Worker. (The legacy zone gets a
# redirect variant instead — see step_dns_legacy.)
deploy_cf_target() {
  local token="$1" account="$2" zone_name="$3"
  local frontend_domain="$4" api_domain="$5" blob_domain="$6" web_service="$7"
  local zone api_host web_host
  zone="$(curl -s -H "Authorization: Bearer $token" \
    "https://api.cloudflare.com/client/v4/zones?name=$zone_name" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"][0]["id"])')"
  api_host="$(gc run services describe "$API_SERVICE" --region="$REGION" --format='value(status.url)' | sed 's#https://##')"
  web_host="$(gc run services describe "$web_service" --region="$REGION" --format='value(status.url)' | sed 's#https://##')"

  # Proxied (orange-cloud) placeholder AAAA records so the Worker route intercepts.
  for name in "${frontend_domain%%.*}" "${api_domain%%.*}"; do
    curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$zone/dns_records" \
      -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
      --data "{\"type\":\"AAAA\",\"name\":\"$name\",\"content\":\"100::\",\"proxied\":true}" >/dev/null
  done

  # Reverse-proxy Worker: rewrite Host to the run.app origin (Cloud Run routes by host).
  local worker; worker="$(mktemp).js"
  cat > "$worker" <<EOF
const MAP = {
  "$frontend_domain": "$web_host",
  "$api_domain": "$api_host",
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
  for name in "$frontend_domain" "$api_domain"; do
    curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$zone/workers/routes" \
      -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
      --data "{\"pattern\":\"$name/*\",\"script\":\"$WORKER_NAME\"}" >/dev/null
  done

  put_blob_target "$token" "$account" "$zone" "$blob_domain"
}

# put_blob_target TOKEN ACCOUNT ZONE_ID BLOB_DOMAIN
# Blob proxy: a Worker that reverse-proxies the public GCS bucket behind
# the blob domain so blob reads are edge-cached. It forwards the path to
# storage.googleapis.com/$BUCKET_NAME/... and lets Cloudflare cache per the
# origin Cache-Control (immutable v2/{hash} blobs cache ~1y at the edge). The
# short-lived manifest.json is ALSO edge-cached (cacheEverything honors its
# origin max-age=60), but the Worker rewrites the client-facing Cache-Control
# back to max-age=60 so the zone's Browser Cache TTL floor cannot stretch it —
# the Worker's own response header wins over the floor, verified live. This
# lets co-located event visitors share one edge-cached manifest per PoP
# instead of each paying an origin RTT.
put_blob_target() {
  local token="$1" account="$2" zone="$3" blob_domain="$4"
  curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$zone/dns_records" \
    -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
    --data "{\"type\":\"AAAA\",\"name\":\"${blob_domain%%.*}\",\"content\":\"100::\",\"proxied\":true}" >/dev/null
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
    --data "{\"pattern\":\"$blob_domain/*\",\"script\":\"$BLOB_WORKER_NAME\"}" >/dev/null
}

step_dns_pp() {
  local token account
  token="$(require_env POPCORNPENGUINS_CLOUDFLARE_API_TOKEN)"
  account="$(require_env POPCORNPENGUINS_CLOUDFLARE_ACCOUNT_ID)"
  deploy_cf_target "$token" "$account" "$CF_ZONE_NAME" \
    "$FRONTEND_DOMAIN" "$API_DOMAIN" "$BLOB_DOMAIN" "$WEB_SERVICE"
}

# Legacy zone (iterativerefinement.com) edge config, cut over 2026-08-29:
#   - $LEGACY_FRONTEND_DOMAIN  -> redirect ($REDIRECT_STATUS, default 301) to
#     https://$FRONTEND_DOMAIN preserving path + query
#   - $LEGACY_API_DOMAIN       -> proxies the backend (legacy API clients)
#   - $LEGACY_BLOB_DOMAIN      -> proxies the bucket (old tabs, blob readers)
# Create-only for DNS records/routes; the only overwrite is the PUT of the two
# statbotics-* Worker scripts in acct #1. Nothing else in the zone is touched.
step_dns_legacy() {
  local token account zone api_host
  token="$(require_env CLOUDFLARE_API_TOKEN)"
  account="$(require_env CLOUDFLARE_ACCOUNT_ID)"
  zone="$(curl -s -H "Authorization: Bearer $token" \
    "https://api.cloudflare.com/client/v4/zones?name=$LEGACY_CF_ZONE_NAME" \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["result"][0]["id"])')"
  api_host="$(gc run services describe "$API_SERVICE" --region="$REGION" --format='value(status.url)' | sed 's#https://##')"

  for name in "${LEGACY_FRONTEND_DOMAIN%%.*}" "${LEGACY_API_DOMAIN%%.*}"; do
    curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$zone/dns_records" \
      -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
      --data "{\"type\":\"AAAA\",\"name\":\"$name\",\"content\":\"100::\",\"proxied\":true}" >/dev/null
  done

  local worker; worker="$(mktemp).js"
  cat > "$worker" <<EOF
const REDIRECT_HOST = "$LEGACY_FRONTEND_DOMAIN";
const REDIRECT_TO = "https://$FRONTEND_DOMAIN";
const REDIRECT_STATUS = $REDIRECT_STATUS;
const MAP = {
  "$LEGACY_API_DOMAIN": "$api_host",
};
addEventListener("fetch", (e) => e.respondWith(handle(e.request)));
async function handle(request) {
  const url = new URL(request.url);
  if (url.hostname === REDIRECT_HOST) {
    return Response.redirect(REDIRECT_TO + url.pathname + url.search, REDIRECT_STATUS);
  }
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
  for name in "$LEGACY_FRONTEND_DOMAIN" "$LEGACY_API_DOMAIN"; do
    curl -s -X POST "https://api.cloudflare.com/client/v4/zones/$zone/workers/routes" \
      -H "Authorization: Bearer $token" -H "Content-Type: application/json" \
      --data "{\"pattern\":\"$name/*\",\"script\":\"$WORKER_NAME\"}" >/dev/null
  done

  put_blob_target "$token" "$account" "$zone" "$LEGACY_BLOB_DOMAIN"
}

case "${1:-all}" in
  apis) step_apis ;;
  bucket) step_bucket ;;
  secrets) step_secrets ;;
  images) step_images ;;
  backend) step_backend ;;
  seed) step_seed ;;
  frontend) step_frontend ;;
  scheduler) step_scheduler ;;
  dns-pp) step_dns_pp ;;
  dns-legacy) step_dns_legacy ;;
  all)
    step_apis; step_bucket; step_secrets; step_images
    step_backend; step_seed; step_frontend; step_scheduler; step_dns_pp; step_dns_legacy ;;
  *) echo "unknown step: $1" >&2; exit 1 ;;
esac
echo "OK: ${1:-all}"
