# Statbotics staging mirror — operational targets.
#
# Codifies the deterministic steps to build, deploy, and rebuild data on the
# staging mirror (statbotics.iterativerefinement.com) so they don't have to be
# remembered or re-derived. Wraps deploy.sh for build/deploy and adds targeted
# data-rebuild + operational targets.
#
# Architecture + first-time setup:  see DEPLOY.md
# Full-history rebuild + reseed:     see ../../deliverables/historical-backfill.md
# What "offseason" means in the app: see DEPLOY.md (§4)
#
# Prereqs: gcloud auth (chondl@gmail.com), ADC present, ~/thebluealliance_api_key.txt.
# All vars below can be overridden on the command line, e.g. `make deploy REGION=us-east1`.
#
# Common flows:
#   make ship                      # build both images + deploy both services (code change)
#   make reprocess-curr-year       # re-ingest current year (picks up new offseason events)
#   make reprocess-year YEAR=2025  # re-ingest one historical year (clear+process+parquet+hist blobs)
#   make verify-event EVENT=2026iri
#   make smoke

SHELL := /bin/bash
.ONESHELL:

# ------------------------------- CONFIG (mirrors deploy.sh) -------------------
PROJECT_ID   ?= statbotics-staging
REGION       ?= us-central1
AR_REPO      ?= statbotics
API_SERVICE  ?= statbotics-api
WEB_SERVICE  ?= statbotics-web
DB_INSTANCE  ?= statbotics-staging-db
DB_NAME      ?= statbotics3
DB_USER      ?= statbotics
BUCKET_NAME  ?= statbotics-staging-site
API_DOMAIN   ?= api-statbotics.iterativerefinement.com
BLOB_DOMAIN  ?= blobs-statbotics.iterativerefinement.com
FRONTEND_DOMAIN ?= statbotics.iterativerefinement.com
API_MEMORY   ?= 8Gi
API_CPU      ?= 2
UPDATE_SCHED ?= statbotics-update
GC_SCHED     ?= statbotics-gc

# Build/deploy the DEPLOY BRANCH checkout, not the main worktree. docs/ (where
# this Makefile lives) is git-excluded and only exists in the main checkout on
# `master`, but the code + Dockerfiles that get deployed live on the `staging`
# branch — its worktree at <main>/.worktrees/staging. MAIN_DIR is 4 up from
# here; DEPLOY_DIR is the staging worktree. Override DEPLOY_DIR to deploy a
# different checkout.
MAIN_DIR     ?= $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../../../..)
DEPLOY_DIR   ?= $(MAIN_DIR)/.worktrees/staging
BACKEND_DIR  ?= $(DEPLOY_DIR)/backend
FRONTEND_DIR ?= $(DEPLOY_DIR)/frontend
SMOKE        ?= $(MAIN_DIR)/docs/superpowers/rig/smoke/smoke.py

CONN_NAME    := $(PROJECT_ID):$(REGION):$(DB_INSTANCE)
DATABASE_URL := postgresql+psycopg2://$(DB_USER)@/$(DB_NAME)?host=/cloudsql/$(CONN_NAME)
IMAGE_API    := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(AR_REPO)/$(API_SERVICE):latest
IMAGE_WEB    := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(AR_REPO)/$(WEB_SERVICE):latest
GC           := gcloud --project $(PROJECT_ID)
API_URL      := https://$(API_DOMAIN)

.DEFAULT_GOAL := help

# --------------------------------- BUILD -------------------------------------
.PHONY: build build-api build-web
build: build-api build-web ## Build both container images (Cloud Build -> Artifact Registry)

build-api: ## Build the backend image
	cd $(BACKEND_DIR) && $(GC) builds submit --tag $(IMAGE_API) --timeout=1200 .

build-web: ## Build the frontend image (BACKEND_URL/BUCKET_URL are inlined at build time)
	cfg=$$(mktemp).yaml; \
	printf 'steps:\n  - name: gcr.io/cloud-builders/docker\n    args: [build, --build-arg=BACKEND_URL=https://$(API_DOMAIN)/v3/site, --build-arg=BUCKET_URL=https://$(BLOB_DOMAIN), --build-arg=PROD=True, -t, $(IMAGE_WEB), .]\nimages: [$(IMAGE_WEB)]\noptions: {machineType: E2_HIGHCPU_8}\n' > $$cfg; \
	cd $(FRONTEND_DIR) && $(GC) builds submit --config=$$cfg --timeout=1800 .; \
	rm -f $$cfg

# --------------------------------- DEPLOY ------------------------------------
# Image-only updates: preserve the service's existing env/secrets/Cloud SQL/resources.
.PHONY: deploy deploy-api deploy-web
deploy: deploy-api deploy-web ## Deploy both images to Cloud Run (image-only, preserves config)

deploy-api: ## Roll the backend service to the latest image
	$(GC) run services update $(API_SERVICE) --region $(REGION) --image $(IMAGE_API)

deploy-web: ## Roll the frontend service to the latest image
	$(GC) run services update $(WEB_SERVICE) --region $(REGION) --image $(IMAGE_WEB)

.PHONY: ship
ship: build deploy ## Build both images and deploy both services (the standard code-change flow)

# --------------------------- DATA: CURRENT YEAR ------------------------------
.PHONY: reprocess-curr-year update-curr-year
reprocess-curr-year: ## Full current-year recompute (re-ingests fresh from TBA; picks up new offseason events). ~4-6 min; 524 at the edge is fine, it continues server-side.
	@echo "Triggering full current-year recompute (reset_curr_year)..."
	curl -s --max-time 900 -w '\nHTTP %{http_code}\n' "$(API_URL)/v3/data/reset_curr_year" || true
	@echo "If the edge returned 524, poll: make logs | grep 'Write DB'"

update-curr-year: ## Cheap incremental current-year update (etag pre-check, same as the hourly cron)
	curl -s --max-time 300 -w '\nHTTP %{http_code}\n' "$(API_URL)/v3/site/update_curr_year"

# --------------------------- DATA: HISTORICAL YEAR ---------------------------
# Targeted single-year reprocess as a Cloud Run JOB (jobs allow long timeouts;
# the service HTTP path caps at 3600s). clear_year + process_year(partial=False)
# re-ingests that year fresh (new offseason events) and regenerates its Parquet;
# backfill_blobs republishes its hist/ site blobs for the frontend. Prefer this
# over a full `reset_all_years` when only one year changed.
JOB_NAME ?= statbotics-reprocess-$(YEAR)
REPROCESS_DRIVER = import src.db.models, src.data.main as dm, backfill_blobs; from src.data.utils import create_objs; from src.data.tba import load_teams; from src.db.functions.clear_year import clear_year; t=load_teams(cache=True); clear_year($(YEAR)); dm.process_year($(YEAR), False, False, True, t, create_objs($(YEAR)), None); backfill_blobs.main(['$(YEAR)','--force']); print('REPROCESS_$(YEAR)_COMPLETE')

.PHONY: reprocess-year
reprocess-year: require-YEAR pause-cron ## Reprocess ONE historical year end-to-end. Usage: make reprocess-year YEAR=2025
	@set -e; \
	echo "Reprocessing $(YEAR) (clear + process + parquet + hist blobs) as a Cloud Run job..."; \
	$(GC) run jobs delete $(JOB_NAME) --region $(REGION) --quiet 2>/dev/null || true; \
	$(GC) run jobs create $(JOB_NAME) \
	  --image $(IMAGE_API) --region $(REGION) \
	  --set-cloudsql-instances $(CONN_NAME) \
	  --set-env-vars "^|^PROD=True|GCS_BUCKET=$(BUCKET_NAME)|DATABASE_URL=$(DATABASE_URL)" \
	  --set-secrets "TBA_AUTH_KEY=tba-auth-key:latest,PGPASSWORD=db-password:latest" \
	  --command python --args "^@@@^-c@@@$(REPROCESS_DRIVER)" \
	  --cpu 2 --memory $(API_MEMORY) --task-timeout 3000 --max-retries 0; \
	$(GC) run jobs execute $(JOB_NAME) --region $(REGION) --wait; \
	$(MAKE) resume-cron

.PHONY: backfill-parquet backfill-blobs
backfill-parquet: require-YEAR ## Republish one year's Parquet from the DB (no recompute). YEAR=2015
	$(GC) run jobs delete statbotics-bfparquet-$(YEAR) --region $(REGION) --quiet 2>/dev/null || true
	$(GC) run jobs create statbotics-bfparquet-$(YEAR) --image $(IMAGE_API) --region $(REGION) \
	  --set-cloudsql-instances $(CONN_NAME) \
	  --set-env-vars "^|^PROD=True|GCS_BUCKET=$(BUCKET_NAME)|DATABASE_URL=$(DATABASE_URL)" \
	  --set-secrets "TBA_AUTH_KEY=tba-auth-key:latest,PGPASSWORD=db-password:latest" \
	  --command python --args "backfill_parquet.py@@@$(YEAR)@@@--force" --args-delimiter @@@ \
	  --cpu 2 --memory $(API_MEMORY) --task-timeout 1800 --max-retries 0
	$(GC) run jobs execute statbotics-bfparquet-$(YEAR) --region $(REGION) --wait

backfill-blobs: require-YEAR ## Republish one year's hist/ site blobs from the DB. YEAR=2015
	$(GC) run jobs delete statbotics-bfblobs-$(YEAR) --region $(REGION) --quiet 2>/dev/null || true
	$(GC) run jobs create statbotics-bfblobs-$(YEAR) --image $(IMAGE_API) --region $(REGION) \
	  --set-cloudsql-instances $(CONN_NAME) \
	  --set-env-vars "^|^PROD=True|GCS_BUCKET=$(BUCKET_NAME)|DATABASE_URL=$(DATABASE_URL)" \
	  --set-secrets "TBA_AUTH_KEY=tba-auth-key:latest,PGPASSWORD=db-password:latest" \
	  --command python --args "backfill_blobs.py@@@$(YEAR)@@@--force" --args-delimiter @@@ \
	  --cpu 2 --memory $(API_MEMORY) --task-timeout 1800 --max-retries 0
	$(GC) run jobs execute statbotics-bfblobs-$(YEAR) --region $(REGION) --wait

# --------------------------------- SCHEDULER ---------------------------------
# Pause the hourly update + daily GC around a manual data rebuild so their
# manifest writes cannot race the rebuild's.
.PHONY: pause-cron resume-cron
pause-cron: ## Pause the hourly update + daily GC schedulers
	-$(GC) scheduler jobs pause $(UPDATE_SCHED) --location $(REGION)
	-$(GC) scheduler jobs pause $(GC_SCHED) --location $(REGION)

resume-cron: ## Resume the schedulers
	-$(GC) scheduler jobs resume $(UPDATE_SCHED) --location $(REGION)
	-$(GC) scheduler jobs resume $(GC_SCHED) --location $(REGION)

# --------------------------------- VERIFY ------------------------------------
.PHONY: smoke verify-event logs info
smoke: ## Run the smoke suite against the mirror
	python3 $(SMOKE) --base-url $(API_URL) --data-url $(API_URL) \
	  --gcs https://storage.googleapis.com --bucket $(BUCKET_NAME) --year $${YEAR:-2026}

verify-event: require-EVENT ## Curl an event + its matches. Usage: make verify-event EVENT=2026iri
	@echo "== $(EVENT) =="
	@curl -s --max-time 20 "$(API_URL)/v3/event/$(EVENT)" | python3 -m json.tool | grep -E '"name"|"week"|"type"|"status"|"num_teams"' || echo "  (not found / 500)"
	@echo "== matches =="
	@curl -s --max-time 20 "$(API_URL)/v3/matches?event=$(EVENT)" | python3 -c "import json,sys; m=json.load(sys.stdin); print('  count:', len(m) if isinstance(m,list) else m)"

logs: ## Tail recent backend logs
	$(GC) run services logs read $(API_SERVICE) --region $(REGION) --limit $${N:-30}

info: ## Show the deployed backend /info
	@curl -s --max-time 15 "$(API_URL)/info" | python3 -m json.tool

# --------------------------------- GUARDS / HELP -----------------------------
.PHONY: require-YEAR require-EVENT help
require-YEAR:
	@test -n "$(YEAR)" || { echo "ERROR: set YEAR=<year>, e.g. make $(MAKECMDGOALS) YEAR=2025"; exit 1; }
require-EVENT:
	@test -n "$(EVENT)" || { echo "ERROR: set EVENT=<key>, e.g. make $(MAKECMDGOALS) EVENT=2026iri"; exit 1; }

help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
