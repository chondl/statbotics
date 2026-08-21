# Statbotics staging mirror — operational targets.
#
# Codifies the deterministic steps to build, deploy, and rebuild data on the
# staging mirrors (statbotics.iterativerefinement.com and
# statbotics.popcornpenguins.com — one shared backend/bucket, one frontend
# service per domain) so they don't have to be remembered or re-derived. Wraps
# deploy.sh for build/deploy and adds targeted data-rebuild + operational
# targets.
#
# Architecture + first-time setup:  see DEPLOY.md
# Full-history rebuild + reseed:     see ../../deliverables/historical-backfill.md
# What "offseason" means in the app: see DEPLOY.md (§4)
#
# Prereqs: gcloud auth (chondl@gmail.com), ADC present. Secrets come ONLY from
# environment variables ($TBA_AUTH_KEY etc. — see deploy.sh header), injected
# from the operator's Keychain-backed environment; there are no secret files.
# All vars below can be overridden on the command line, e.g. `make deploy REGION=us-east1`.
#
# ============================================================================
#  THERE IS EXACTLY ONE WAY TO DEPLOY:  make ship
# ============================================================================
#  Do not hand-roll `gcloud builds submit` / `gcloud run deploy`. Do not build
#  or deploy one service "because only the backend changed". Do not reach for
#  build-api/build-web/build-web-pp/deploy-api/deploy-web/deploy-web-pp
#  directly — they are GUARDED and will refuse to run outside `make ship`
#  (see require-ship below).
#
#  Why this is a hard rule and not a preference: partial and hand-rolled
#  deploys are how production silently drifts from branch history. An
#  image-only `gcloud run deploy` typed by hand also drops env/secrets that
#  `make deploy` preserves. Reasoning "the frontend didn't change, so I'll
#  skip it" is exactly the judgment call this rule exists to remove — it is
#  cheap to rebuild an unchanged image and expensive to be wrong.
#
#  If you genuinely need a partial deploy, you must opt in explicitly and say
#  why:  make deploy-api ALLOW_PARTIAL=1
# ============================================================================
#
# Common flows:
#   make ship                      # THE deploy path: build both images + deploy both services
#   make reprocess-curr-year       # re-ingest current year (picks up new offseason events)
#   make reprocess-year YEAR=2025  # re-ingest one historical year (process+parquet+hist blobs)
#   make verify-event EVENT=2026iri
#   make smoke

# DANGER — never dry-run with `make -n` here: with .ONESHELL, any recipe that
# mentions $(MAKE) is EXECUTED in full even under -n. (This bit us on
# 2026-07-21: an intended dry run cleared+reprocessed 2025.) No recipe uses
# $(MAKE) anymore — resume-cron is inlined in reprocess-year, which ALSO fixes
# a second bug: $(MAKE) submakes do not inherit -f, so the chained resume
# silently failed and left all four schedulers paused. Keep it that way.
SHELL := /bin/bash
.ONESHELL:

# ------------------------------- CONFIG (mirrors deploy.sh) -------------------
PROJECT_ID   ?= statbotics-staging
REGION       ?= us-central1
AR_REPO      ?= statbotics
API_SERVICE  ?= statbotics-api
WEB_SERVICE  ?= statbotics-web
WEB_PP_SERVICE ?= statbotics-web-pp
BUCKET_NAME  ?= statbotics-staging-site
API_DOMAIN   ?= api-statbotics.iterativerefinement.com
BLOB_DOMAIN  ?= blobs-statbotics.iterativerefinement.com
FRONTEND_DOMAIN ?= statbotics.iterativerefinement.com
PP_API_DOMAIN   ?= api-statbotics.popcornpenguins.com
PP_BLOB_DOMAIN  ?= blobs-statbotics.popcornpenguins.com
PP_FRONTEND_DOMAIN ?= statbotics.popcornpenguins.com
API_MEMORY   ?= 8Gi
API_CPU      ?= 2
UPDATE_SCHED ?= statbotics-update
GC_SCHED     ?= statbotics-gc
TBA_SWEEP_SCHED ?= statbotics-tba-sweep
REFRESH_TEAMS_SCHED ?= statbotics-refresh-teams
BILLING_ACCT ?= 01DAA6_73BA7B_54EFE1
BILLING_TABLE ?= $(PROJECT_ID).billing_export.gcp_billing_export_v1_$(BILLING_ACCT)
DAYS         ?= 30

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

IMAGE_API    := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(AR_REPO)/$(API_SERVICE):latest
IMAGE_WEB    := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(AR_REPO)/$(WEB_SERVICE):latest
IMAGE_WEB_PP := $(REGION)-docker.pkg.dev/$(PROJECT_ID)/$(AR_REPO)/$(WEB_PP_SERVICE):latest
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

# The partial-flow guard. `ship` sets SHIP := 1 as a TARGET-SPECIFIC variable,
# which GNU make propagates to all of that target's prerequisites — so the
# build-*/deploy-* targets can tell whether they were reached through `ship`
# or invoked directly. (Verified on GNU Make 3.81, the macOS system make.)
#
# This deliberately blocks the "only the backend changed, so I'll just roll the
# api" shortcut. Rebuilding an unchanged image is nearly free; shipping half a
# revision pair, or hand-rolling the gcloud call and dropping env/secrets, is
# not. Override only with a conscious ALLOW_PARTIAL=1 on the command line.
.PHONY: require-ship
require-ship:
	@test -n "$(SHIP)$(ALLOW_PARTIAL)" || { \
	  echo "ERROR: '$(MAKECMDGOALS)' is a PARTIAL deploy step — use 'make ship'."; \
	  echo ""; \
	  echo "  make ship   builds ALL images and rolls ALL services (backend + one"; \
	  echo "              frontend per mirror domain), which is the"; \
	  echo "              only sanctioned way to deploy this mirror. Partial and"; \
	  echo "              hand-rolled deploys are how production drifts from branch"; \
	  echo "              history. See DEPLOY.md section 1."; \
	  echo ""; \
	  echo "  If you really mean a partial deploy, opt in explicitly:"; \
	  echo "      make $(MAKECMDGOALS) ALLOW_PARTIAL=1"; \
	  exit 1; \
	}

.PHONY: build build-api build-web build-web-pp
build: require-ship build-api build-web build-web-pp ## (internal — use `make ship`) Build all three container images

build-api: require-ship check-deploy-src ## (internal — use `make ship`) Build the backend image
	cd $(BACKEND_DIR) && $(GC) builds submit --tag $(IMAGE_API) --timeout=1200 .

# One frontend image per mirror: BACKEND_URL/BUCKET_URL are inlined into the JS
# bundle at `next build` time, so each domain needs its own build of the SAME code.
build-web: require-ship check-deploy-src ## (internal — use `make ship`) Build the iterativerefinement frontend image
	cfg=$$(mktemp).yaml; \
	printf 'steps:\n  - name: gcr.io/cloud-builders/docker\n    args: [build, --build-arg=BACKEND_URL=https://$(API_DOMAIN)/v3/site, --build-arg=BUCKET_URL=https://$(BLOB_DOMAIN), --build-arg=PROD=True, -t, $(IMAGE_WEB), .]\nimages: [$(IMAGE_WEB)]\noptions: {machineType: E2_HIGHCPU_8}\n' > $$cfg; \
	cd $(FRONTEND_DIR) && $(GC) builds submit --config=$$cfg --timeout=1800 .; \
	rm -f $$cfg

build-web-pp: require-ship check-deploy-src ## (internal — use `make ship`) Build the popcornpenguins frontend image
	cfg=$$(mktemp).yaml; \
	printf 'steps:\n  - name: gcr.io/cloud-builders/docker\n    args: [build, --build-arg=BACKEND_URL=https://$(PP_API_DOMAIN)/v3/site, --build-arg=BUCKET_URL=https://$(PP_BLOB_DOMAIN), --build-arg=PROD=True, -t, $(IMAGE_WEB_PP), .]\nimages: [$(IMAGE_WEB_PP)]\noptions: {machineType: E2_HIGHCPU_8}\n' > $$cfg; \
	cd $(FRONTEND_DIR) && $(GC) builds submit --config=$$cfg --timeout=1800 .; \
	rm -f $$cfg

# --------------------------------- DEPLOY ------------------------------------
# Image-only updates: preserve the service's existing env/secrets/resources.
# NEVER hand-roll `gcloud run deploy` instead of these — a typed-out deploy
# re-specifies the container and silently drops env vars and secrets that this
# image-only `services update` preserves.
.PHONY: deploy deploy-api deploy-web deploy-web-pp
deploy: require-ship deploy-api deploy-web deploy-web-pp ## (internal — use `make ship`) Deploy all three images to Cloud Run

deploy-api: require-ship ## (internal — use `make ship`) Roll the backend service to the latest image
	$(GC) run services update $(API_SERVICE) --region $(REGION) --image $(IMAGE_API)

deploy-web: require-ship ## (internal — use `make ship`) Roll the iterativerefinement frontend service to the latest image
	$(GC) run services update $(WEB_SERVICE) --region $(REGION) --image $(IMAGE_WEB)

deploy-web-pp: require-ship ## (internal — use `make ship`) Roll the popcornpenguins frontend service to the latest image
	$(GC) run services update $(WEB_PP_SERVICE) --region $(REGION) --image $(IMAGE_WEB_PP)

# THE deploy path. Everything above is a component of this and is guarded so it
# cannot be invoked piecemeal by accident.
.PHONY: ship
ship: SHIP := 1
ship: build deploy ## ⭐ THE ONLY WAY TO DEPLOY: build all three images + roll all three services

# ---------------------------- DB RETIREMENT (DONE) ----------------------------
# There is no database, and no mode flag either. Retirement completed
# 2026-07-27: the Cloud SQL instance, the `db-password` secret, and the
# `cloudsql.client` grant were deleted, then the DB code and finally the
# `DISABLE_DB` flag itself. Serving and persistence are DuckDB-over-Parquet +
# GCS blobs, unconditionally. If you are looking for a `flip-db` target to
# switch modes: there are no modes.

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
refresh-teams: ## Manually run the daily team-fields refresh (TBA team re-sync + targeted artifact republish). Normally the statbotics-refresh-teams scheduler runs this at 06:30 UTC.
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
# The retired backfill_parquet.py / backfill_blobs.py operator scripts are
# superseded by this driver + reset_all_years (see
# ../../deliverables/historical-backfill.md).
JOB_NAME ?= statbotics-reprocess-$(YEAR)
REPROCESS_DRIVER = import src.db.models, src.data.main as dm; dm.reprocess_year($(YEAR)); print('REPROCESS_$(YEAR)_COMPLETE (parquet + hist blobs + chained curr-year re-render)')

.PHONY: reprocess-year
reprocess-year: require-YEAR pause-cron ## Reprocess ONE historical year end-to-end, then chain a full current-year re-render. Usage: make reprocess-year YEAR=2025 [REFRESH_TBA=1 to bypass the TBA cache manifest]
	@set -e; \
	echo "Reprocessing $(YEAR) (process + parquet + hist blobs) as a Cloud Run job..."; \
	$(GC) run jobs delete $(JOB_NAME) --region $(REGION) --quiet 2>/dev/null || true; \
	$(GC) run jobs create $(JOB_NAME) \
	  --image $(IMAGE_API) --region $(REGION) \
	  --set-env-vars "^|^PROD=True|GCS_BUCKET=$(BUCKET_NAME)$(if $(REFRESH_TBA),|REFRESH_TBA=$(REFRESH_TBA))" \
	  --set-secrets "TBA_AUTH_KEY=tba-auth-key:latest" \
	  --command python --args "^@@@^-c@@@$(REPROCESS_DRIVER)" \
	  --cpu 2 --memory $(API_MEMORY) --task-timeout 3600 --max-retries 0; \
	$(GC) run jobs execute $(JOB_NAME) --region $(REGION) --wait; \
	for j in $(UPDATE_SCHED) $(GC_SCHED) $(TBA_SWEEP_SCHED) $(REFRESH_TEAMS_SCHED); do \
	  $(GC) scheduler jobs resume $$j --location $(REGION) || true; \
	done

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
.PHONY: smoke verify-web verify-event logs info
smoke: ## Run the smoke suite against the mirror
	python3 $(SMOKE) --base-url $(API_URL) --data-url $(API_URL) \
	  --gcs https://storage.googleapis.com --bucket $(BUCKET_NAME) --year $${YEAR:-2026}

verify-web: ## Curl both frontend mirrors + both API hostnames (expect HTTP 200 each)
	@for u in https://$(FRONTEND_DOMAIN)/ https://$(PP_FRONTEND_DOMAIN)/ https://$(API_DOMAIN)/v3/year/2026 https://$(PP_API_DOMAIN)/v3/year/2026; do \
	  code=$$(curl -s -o /dev/null -w '%{http_code}' --max-time 30 "$$u"); \
	  echo "  $$u -> $$code"; \
	done

verify-event: require-EVENT ## Curl an event + its matches. Usage: make verify-event EVENT=2026iri
	@echo "== $(EVENT) =="
	@curl -s --max-time 20 "$(API_URL)/v3/event/$(EVENT)" | python3 -m json.tool | grep -E '"name"|"week"|"type"|"status"|"num_teams"' || echo "  (not found / 500)"
	@echo "== matches =="
	@curl -s --max-time 20 "$(API_URL)/v3/matches?event=$(EVENT)" | python3 -c "import json,sys; m=json.load(sys.stdin); print('  count:', len(m) if isinstance(m,list) else m)"

logs: ## Tail recent backend logs
	$(GC) run services logs read $(API_SERVICE) --region $(REGION) --limit $${N:-30}

info: ## Show the deployed backend /info
	@curl -s --max-time 15 "$(API_URL)/info" | python3 -m json.tool

# --------------------------------- BILLING -----------------------------------
# Actual accrued spend, from the BigQuery billing export (see ../BILLING.md).
# There is no gcloud/API path to real cost — only pricing and budgets — so these
# targets query the export directly.
#
# Two credit kinds reduce the bill and are broken out separately, because they
# expire on completely different terms: type DISCOUNT is the Cloud Run free
# tier (renews monthly, permanent), type PROMOTION is the one-time free-trial
# credit (finite — when it runs out, `gross` becomes the actual invoice). Read
# the `gross` column as "what this would cost us today with no credits at all".
#
# The last day is always partial: billing rows lag the export by a few hours.
#
# The SQL is kept on ONE physical line per target. macOS ships GNU Make 3.81,
# where the `.ONESHELL:` above is silently ignored (it landed in 3.82) — each
# recipe line is its own shell, so a multi-line quoted string dies with
# "unexpected EOF while looking for matching `''". The fragments below keep it
# readable without multi-line recipes.
CREDS_ALL   = (SELECT IFNULL(SUM(c.amount),0) FROM UNNEST(credits) c)
CREDS_FREE  = (SELECT IFNULL(SUM(c.amount),0) FROM UNNEST(credits) c WHERE c.type="DISCOUNT")
CREDS_PROMO = (SELECT IFNULL(SUM(c.amount),0) FROM UNNEST(credits) c WHERE c.type="PROMOTION")
BILL_WINDOW = WHERE DATE(usage_start_time) >= DATE_SUB(CURRENT_DATE(), INTERVAL $(DAYS) DAY)
BQ          = bq query --use_legacy_sql=false --project_id=$(PROJECT_ID) --format=pretty

.PHONY: bill bill-services
bill: ## Per-day gross / free-tier / trial-credit / net spend. Usage: make bill [DAYS=30]
	@$(BQ) 'SELECT DATE(usage_start_time) AS day, ROUND(SUM(cost),2) AS gross, ROUND(SUM($(CREDS_FREE)),2) AS free_tier, ROUND(SUM($(CREDS_PROMO)),2) AS trial_credit, ROUND(SUM($(CREDS_ALL)),2) AS credits, ROUND(SUM(cost)+SUM($(CREDS_ALL)),2) AS net FROM `$(BILLING_TABLE)` $(BILL_WINDOW) GROUP BY day ORDER BY day'

bill-services: ## Gross spend per service over the window. Usage: make bill-services [DAYS=30]
	@$(BQ) 'SELECT service.description AS service, ROUND(SUM(cost),2) AS gross, ROUND(SUM(cost)/COUNT(DISTINCT DATE(usage_start_time)),3) AS per_day FROM `$(BILLING_TABLE)` $(BILL_WINDOW) GROUP BY service HAVING gross > 0 ORDER BY gross DESC'

# ------------------------- ARTIFACT REGISTRY CLEANUP -------------------------
# Every `make ship` pushes a new statbotics-api + statbotics-web image and moves
# the `:latest` tag off the previous one, so the repo accumulates untagged
# versions forever. By 2026-07-30 that was 59 versions / 34.9 GB and $0.11/day
# — the only cost line that grows monotonically, and the largest recurring one
# left after Cloud SQL was decommissioned (see ../BILLING.md).
#
# The policy in artifact-cleanup-policy.json deletes untagged versions older
# than 7 days, with two Keep rules in front of it. Keep beats Delete in
# Artifact Registry regardless of rule order, so the 5 newest versions of each
# package and whatever currently carries `:latest` are never eligible.
#
# Why that is safe: Cloud Run pins each revision to a DIGEST, and the digest
# serving traffic is always the one `:latest` resolved to at deploy time — so
# the live image is protected twice over (newest + tagged). Reprocess jobs are
# created from `:latest` on demand and deleted after, so nothing else pins an
# old digest. What this DOES give up is `gcloud run services update-traffic`
# rollback to revisions older than the keep window; the rollback path is to
# rebuild from the cph-staging branch, which is the documented one anyway.
CLEANUP_POLICY := $(dir $(lastword $(MAKEFILE_LIST)))artifact-cleanup-policy.json
AR_CMD          = $(GC) artifacts repositories

.PHONY: cleanup-policy cleanup-policy-dry-run cleanup-policy-show
cleanup-policy: ## Apply the Artifact Registry cleanup policy (ENFORCING — deletes images)
	$(AR_CMD) set-cleanup-policies $(AR_REPO) --location $(REGION) --policy $(CLEANUP_POLICY) --no-dry-run

cleanup-policy-dry-run: ## Apply the cleanup policy in dry-run mode (logs what it would delete, deletes nothing)
	$(AR_CMD) set-cleanup-policies $(AR_REPO) --location $(REGION) --policy $(CLEANUP_POLICY) --dry-run

cleanup-policy-show: ## Show the live cleanup policy, dry-run flag, and current repo size
	@$(AR_CMD) describe $(AR_REPO) --location $(REGION) --format="yaml(cleanupPolicies,cleanupPolicyDryRun,sizeBytes)"

# --------------------------------- GUARDS / HELP -----------------------------
.PHONY: require-YEAR require-EVENT help
require-YEAR:
	@test -n "$(YEAR)" || { echo "ERROR: set YEAR=<year>, e.g. make $(MAKECMDGOALS) YEAR=2025"; exit 1; }
require-EVENT:
	@test -n "$(EVENT)" || { echo "ERROR: set EVENT=<key>, e.g. make $(MAKECMDGOALS) EVENT=2026iri"; exit 1; }

help: ## List targets
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | sort | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-22s\033[0m %s\n", $$1, $$2}'
