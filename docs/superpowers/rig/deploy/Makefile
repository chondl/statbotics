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
#   make reprocess-year YEAR=2025  # re-ingest one historical year (process+parquet+hist blobs)
#   make verify-event EVENT=2026iri
#   make smoke

# DANGER — never dry-run with `make -n` here: with .ONESHELL, any recipe that
# mentions $(MAKE) (e.g. reprocess-year's resume-cron chain) is EXECUTED in
# full even under -n. A `make -n reprocess-year YEAR=…` really runs the job.
# (This bit us on 2026-07-21: an intended dry run cleared+reprocessed 2025.)
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
TBA_SWEEP_SCHED ?= statbotics-tba-sweep
REFRESH_TEAMS_SCHED ?= statbotics-refresh-teams

# Build/deploy from the repository root checkout, which since 2026-07-20 IS the
# `cph-staging` branch (the deployed fork line) — the former dedicated worktree
# at .worktrees/cph-staging was retired when cph-staging became the primary
# checkout. MAIN_DIR is 4 up from here. The check-deploy-src guard (below)
# refuses to build if the checkout is on another branch or has uncommitted
# backend/frontend changes. DEPLOY_DIR may be overridden to another checkout,
# but the guard still requires it to be a clean cph-staging checkout.
MAIN_DIR     ?= $(abspath $(dir $(lastword $(MAKEFILE_LIST)))/../../../..)
DEPLOY_DIR   ?= $(MAIN_DIR)
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
# Deploys must build exactly what the cph-staging branch records: refuse to
# build from another branch or with uncommitted backend/frontend changes, so
# production can never silently drift from the branch history (every change
# ships via a PR to cph-staging first).
.PHONY: check-deploy-src
check-deploy-src:
	@branch=$$(git -C $(DEPLOY_DIR) branch --show-current); \
	if [ "$$branch" != "cph-staging" ]; then \
	  echo "ERROR: DEPLOY_DIR ($(DEPLOY_DIR)) is on branch '$$branch', not cph-staging."; \
	  echo "Deploys build only from a clean cph-staging checkout (production == branch history, always)."; \
	  exit 1; \
	fi; \
	dirty=$$(git -C $(DEPLOY_DIR) status --porcelain -- backend frontend); \
	if [ -n "$$dirty" ]; then \
	  echo "ERROR: uncommitted backend/ or frontend/ changes in $(DEPLOY_DIR) — land them via a PR to cph-staging before shipping:"; \
	  echo "$$dirty"; \
	  exit 1; \
	fi

.PHONY: build build-api build-web
build: build-api build-web ## Build both container images (Cloud Build -> Artifact Registry)

build-api: check-deploy-src ## Build the backend image
	cd $(BACKEND_DIR) && $(GC) builds submit --tag $(IMAGE_API) --timeout=1200 .

build-web: check-deploy-src ## Build the frontend image (BACKEND_URL/BUCKET_URL are inlined at build time)
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

# ---------------------------- DB RETIREMENT FLIP ------------------------------
# Phase 2 of the DB retirement design (docs/superpowers/specs/
# 2026-07-20-db-retirement-completion-design.md): env-only Cloud Run updates
# that flip the backend between DB and db-less modes. The image and every
# other env/secret/binding are preserved — `make ship` never touches env, so
# the flip survives redeploys until explicitly reversed. The Cloud SQL
# binding stays attached during the 48 h soak (inert with the flag on; it IS
# the rollback path) and is only removed in Phase 4.
.PHONY: flip-dbless flip-db
flip-dbless: ## Flip the backend to db-less (DISABLE_DB=True; env-only, image preserved). Soak 48 h before Cloud SQL decommission. Rollback: make flip-db
	$(GC) run services update $(API_SERVICE) --region $(REGION) --update-env-vars DISABLE_DB=True

flip-db: ## Roll back the db-less flip (DISABLE_DB=False). The DB is stale by the flag-on window — run `make reprocess-curr-year` afterward to rehydrate it.
	$(GC) run services update $(API_SERVICE) --region $(REGION) --update-env-vars DISABLE_DB=False

# --------------------------- DATA: CURRENT YEAR ------------------------------
.PHONY: reprocess-curr-year update-curr-year
reprocess-curr-year: ## Full current-year recompute (re-ingests fresh from TBA; picks up new offseason events). ~4-6 min; 524 at the edge is fine, it continues server-side.
	@echo "Triggering full current-year recompute (reset_curr_year)..."
	curl -s --max-time 900 -w '\nHTTP %{http_code}\n' "$(API_URL)/v3/data/reset_curr_year" || true
	@echo "If the edge returned 524, poll: make logs | grep 'Write Storage'"

update-curr-year: ## Cheap incremental current-year update (etag pre-check, same as the hourly cron)
	curl -s --max-time 300 -w '\nHTTP %{http_code}\n' "$(API_URL)/v3/site/update_curr_year"

.PHONY: tba-sweep
tba-sweep: ## Manually run the daily TBA historical revalidation sweep (one round-robin year, serial conditional GETs; reprocesses the year in-request if anything changed — can take minutes)
	curl -s --max-time 1800 -w '\nHTTP %{http_code}\n' "$(API_URL)/v3/data/revalidate_tba"

.PHONY: refresh-teams
refresh-teams: ## Manually run the daily team-fields refresh (TBA team re-sync; DB writes in DB mode, targeted artifact republish db-less). Normally the statbotics-refresh-teams scheduler runs this at 06:30 UTC.
	curl -s --max-time 1800 -w '\nHTTP %{http_code}\n' "$(API_URL)/v3/data/refresh_teams"

# --------------------------- DATA: HISTORICAL YEAR ---------------------------
# Targeted single-year reprocess as a Cloud Run JOB (jobs allow long timeouts;
# the service HTTP path caps at 3600s). process_year(partial=False) re-ingests
# that year fresh (new offseason events), regenerates its Parquet, and emits its
# hist/ site blobs from pipeline objects (db-less capable). Prefer this over a
# full `reset_all_years` when only one year changed.
#
# The driver is src.data.main.reprocess_year — the SAME function the daily TBA
# sweep runs — so job and sweep can never diverge. It re-ingests the year
# (process_year partial=False), republishes its Parquet, emits its hist/ site
# blobs from the pipeline objects (write-once per HIST_EPOCH; bump HIST_EPOCH
# in src/constants.py to force a re-export), rewrites the year's DB rows in DB
# mode (write_objs clean=True; skipped entirely db-less), and ends by running
# reset_curr_year's body (update_curr_year(partial=False) + refresh_teams) IN
# the job: team/{num} site blobs embed each team's full-history team_years,
# and the team-blob change gate (storage.py _team_render_set) skips them on
# partial cycles on the assumption that historical years only change here — so
# a historical reprocess MUST chain a full current-year re-render or team
# blobs go stale. Chaining inside the driver (not a curl in the recipe) means
# it runs even if the job is executed by hand and cannot be forgotten. Unlike
# `make reprocess-curr-year` there is no HTTP edge here, so the "524 at the
# edge is fine" caveat does not apply — `jobs execute --wait` returns only
# when everything (chain included) finished.
#
# Works in both modes: pass DISABLE_DB=True to run the job db-less (the Cloud
# SQL binding + PGPASSWORD below are then inert — kept until Phase 4 removes
# them). The retired backfill_parquet.py / backfill_blobs.py operator scripts
# are superseded by this driver + a db-less reset_all_years (see
# ../../deliverables/historical-backfill.md).
JOB_NAME ?= statbotics-reprocess-$(YEAR)
REPROCESS_DRIVER = import src.db.models, src.data.main as dm; dm.reprocess_year($(YEAR)); print('REPROCESS_$(YEAR)_COMPLETE (parquet + hist blobs + chained curr-year re-render)')

.PHONY: reprocess-year
reprocess-year: require-YEAR pause-cron ## Reprocess ONE historical year end-to-end, then chain a full current-year re-render. Usage: make reprocess-year YEAR=2025 [REFRESH_TBA=1 to bypass the TBA cache manifest] [DISABLE_DB=True to run db-less]
	@set -e; \
	echo "Reprocessing $(YEAR) (process + parquet + hist blobs) as a Cloud Run job..."; \
	$(GC) run jobs delete $(JOB_NAME) --region $(REGION) --quiet 2>/dev/null || true; \
	$(GC) run jobs create $(JOB_NAME) \
	  --image $(IMAGE_API) --region $(REGION) \
	  --set-cloudsql-instances $(CONN_NAME) \
	  --set-env-vars "^|^PROD=True|GCS_BUCKET=$(BUCKET_NAME)|DATABASE_URL=$(DATABASE_URL)$(if $(REFRESH_TBA),|REFRESH_TBA=$(REFRESH_TBA))$(if $(DISABLE_DB),|DISABLE_DB=$(DISABLE_DB))" \
	  --set-secrets "TBA_AUTH_KEY=tba-auth-key:latest,PGPASSWORD=db-password:latest" \
	  --command python --args "^@@@^-c@@@$(REPROCESS_DRIVER)" \
	  --cpu 2 --memory $(API_MEMORY) --task-timeout 3600 --max-retries 0; \
	$(GC) run jobs execute $(JOB_NAME) --region $(REGION) --wait; \
	$(MAKE) resume-cron

# --------------------------------- SCHEDULER ---------------------------------
# Pause the hourly update + daily GC around a manual data rebuild so their
# manifest writes cannot race the rebuild's.
.PHONY: pause-cron resume-cron
pause-cron: ## Pause the hourly update + daily GC + daily TBA sweep + daily refresh-teams schedulers
	-$(GC) scheduler jobs pause $(UPDATE_SCHED) --location $(REGION)
	-$(GC) scheduler jobs pause $(GC_SCHED) --location $(REGION)
	-$(GC) scheduler jobs pause $(TBA_SWEEP_SCHED) --location $(REGION)
	-$(GC) scheduler jobs pause $(REFRESH_TEAMS_SCHED) --location $(REGION)

resume-cron: ## Resume the schedulers
	-$(GC) scheduler jobs resume $(UPDATE_SCHED) --location $(REGION)
	-$(GC) scheduler jobs resume $(GC_SCHED) --location $(REGION)
	-$(GC) scheduler jobs resume $(TBA_SWEEP_SCHED) --location $(REGION)
	-$(GC) scheduler jobs resume $(REFRESH_TEAMS_SCHED) --location $(REGION)

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
