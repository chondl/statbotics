# Staging — connection & ops

STATUS: LIVE — FULL MIGRATION STACK (verified 2026-07-10)

A full staging instance of statbotics on GCP project `statbotics-staging`, fronted
by Cloudflare. As of 2026-07-10 it runs the **entire migration stack live**:
state-snapshot pipeline, DuckDB-over-Parquet `/v3` serving (`API_BACKEND=duckdb`),
single-manifest blob+Parquet publishing, blob GC, and all track fixes. The DB
stays connected (written each cycle; fallback). This is the operational quick
reference; the running log, cost, teardown, and merge SHAs are in
`docs/superpowers/status/staging.md`. The reproducible build is in
`docs/superpowers/rig/deploy/` (`deploy.sh` + `DEPLOY.md`).

Deployed branch `staging` head `a4a9c8a` (was `eebb3e8`; + quality/perf fixes —
match-from-event-blob, team event-wave decouple, /teams-2021 guard, match
NaN→N/A, 2015 elim-winner derivation; see staging.md). Live revisions: api
`statbotics-api-00010-ln6` (2 vCPU / 8Gi, `API_BACKEND=duckdb`), web
`statbotics-web-00009-jv8`. The CF blob worker now edge-caches `manifest.json`
(~60s) while rewriting its client `Cache-Control` back to `max-age=60`.

## Endpoints

| Component | URL |
|-----------|-----|
| Frontend | https://statbotics.iterativerefinement.com |
| Backend API | https://api-statbotics.iterativerefinement.com |
| Public bucket | https://storage.googleapis.com/statbotics-staging-site (`/manifest.json`) |
| Backend (direct run.app) | https://statbotics-api-630091002690.us-central1.run.app |
| Frontend (direct run.app) | https://statbotics-web-630091002690.us-central1.run.app |

Health: `curl https://api-statbotics.iterativerefinement.com/info`.

## Project / naming

- Project: `statbotics-staging` (always pass `--project=statbotics-staging`).
- Region: `us-central1`.
- Cloud SQL: **none** — deleted 2026-07-27 (DB retirement Phase 4). Final dump
  at `gs://statbotics-staging-db-final-export/` (private).
- Bucket: `statbotics-staging-site` (public read).
- Cloud Run: `statbotics-api` (2 vCPU / 8Gi), `statbotics-web`; jobs
  `statbotics-seed` (db-less stand-up, created on demand by `deploy.sh seed`),
  transient `statbotics-reprocess-{year}` jobs from `make reprocess-year`.
- Scheduler: `statbotics-update` (hourly), `statbotics-gc` (daily 04:30 UTC, v2/ GC).
  How the hourly cron + read-triggered ping drive EPA recompute:
  [DATA-REFRESH.md](DATA-REFRESH.md).

## Full-stack env vars (backend `statbotics-api`)

- `API_BACKEND=duckdb` — `/v3` public API serves from DuckDB-over-Parquet
  (`src/api/backend.py` imports read fns from `src.db_duckdb`). **This is the
  deployed default and now the only option** — leaving it unset would select
  relational-DB serving against a database that no longer exists.
- Snapshot + Parquet publishing are **on by default in the code** (no flag). Each
  cycle writes `state/snapshot.{year}` and folds current-year Parquet into the
  single manifest write.
- `DISABLE_DB=True` — **the deployed production mode since 2026-07-21T16:07Z**,
  and since Phase 4 (2026-07-27) **permanent**: the Cloud SQL instance is gone,
  so this is no longer a flip with a rollback but simply how the service runs.
  It is baked into `deploy.sh`'s `step_backend`, and the `make flip-dbless` /
  `make flip-db` targets have been removed. The backend constructs no
  engine/session, skips all DB writes, and serves `/v3`+`/v3/site` purely from
  Parquet + the snapshot. **Never set `DISABLE_DB=False`** — it would take
  production down. Health signal: `/info` shows `DB_LESS_SEED_INCOMPLETE: false`
  when the historical Parquet backfill is intact (it is: 144 objects, 24 years ×
  6 tables).

## Historical Parquet + hist blobs (pipeline-owned)

DuckDB serves `/v3` for a year from `parquet/{year}/*.parquet`; historical
pages read `hist/{epoch}/…` blobs. Since DB retirement Phase 3 the pipeline
emits both for every historical year it processes (`write_parquet` +
`write_hist_blobs` in `process_year`) — the one-off DB-reading scripts
(`backfill_parquet.py`, `backfill_blobs.py`) and their jobs are retired.
Rebuild one year with `make reprocess-year YEAR=…`; full history via a
db-less `reset_all_years` — see
[historical-backfill.md](../deliverables/historical-backfill.md).
- Cloudflare Worker: `statbotics-proxy` (+ 2 routes, 2 proxied AAAA records).

## Secrets

- Local: `/Users/chondl/statbotics_staging_secret.txt` (chmod 600, KEY=VALUE).
- Secret Manager: `tba-auth-key`. (`db-password` was deleted with Cloud SQL on
  2026-07-27; the local secret file's `CLOUDSQL_*` lines are now dead entries.)
- The backend gets `TBA_AUTH_KEY` (from `tba-auth-key`). Never echo it.

## Common operations

Rebuild + redeploy backend after a code change on branch `staging`:
```bash
cd .worktrees/staging/backend
gcloud --project=statbotics-staging builds submit \
  --tag us-central1-docker.pkg.dev/statbotics-staging/statbotics/statbotics-api:latest .
gcloud --project=statbotics-staging run services update statbotics-api --region=us-central1 \
  --image us-central1-docker.pkg.dev/statbotics-staging/statbotics/statbotics-api:latest
```
Frontend: build `statbotics-web` with build-args `BACKEND_URL`, `BUCKET_URL`,
`PROD=True` (see `docs/superpowers/rig/deploy/deploy.sh step_images`), then
`run services update statbotics-web`.

Re-seed (schema + full current-year build + trailing partial cycle):
```bash
docs/superpowers/rig/deploy/deploy.sh seed
```

Trigger an update cycle manually:
```bash
curl https://statbotics-api-630091002690.us-central1.run.app/v3/data/update_curr_year   # ~62-76s, manifest advances
```

Logs:
```bash
gcloud --project=statbotics-staging logging read \
  'resource.type=cloud_run_revision AND resource.labels.service_name=statbotics-api' --limit=50
```

Smoke test:
```bash
python3 docs/superpowers/rig/smoke/smoke.py \
  --base-url https://statbotics-api-630091002690.us-central1.run.app \
  --data-url https://statbotics-api-630091002690.us-central1.run.app \
  --gcs https://storage.googleapis.com --bucket statbotics-staging-site --year 2026
```
Point `--base-url` at the run.app URL (Cloudflare's WAF 403s urllib's default
User-Agent). 9/9 pass; `--run-update` is 9/10 (check-5 client 60s timeout only —
the cycle takes ~62-76s but completes and the manifest advances).

## Gotchas

- **Backend needs ≥8Gi for a full-stack cycle** (2 vCPU — Cloud Run requires ≥2
  vCPU at 8Gi). The stack runs the ETL cycle *inside* the serving container
  (scheduler → `/v3/site/update_curr_year` backgrounds it), and EPA replay +
  cycle-start deepcopy + 24 MB snapshot serialize + Parquet serialize + the DuckDB
  Parquet cache OOM at 2Gi (peak 2055 MiB) **and** 4Gi (peak 4166 MiB). 8Gi clears
  it (cycle ~139 s). (Pre-stack the plain cycle fit in 2Gi; the snapshot+Parquet
  additions are what pushed it over.) `deploy.sh` `API_MEMORY`/`API_CPU` = 8Gi/2.
- **Manifest 60 s Cache-Control races tight read-modify-write loops.** The manifest
  carries `Cache-Control: public, max-age=60`. Anything that does many rapid
  read-manifest → add-ref → write-manifest cycles (e.g. a naive per-year Parquet
  backfill) reads stale copies and clobbers its own additions. Do a **single
  terminal manifest write** (the pipeline already writes it once per cycle, and
  its per-year historical cadence is minutes, so it never hits this).
- **Cloudflare Host rewrite is required** — a plain proxied CNAME to run.app 404s
  (Cloud Run routes by Host). The `statbotics-proxy` Worker rewrites Host to the
  run.app origin. run.app hostnames are stable across revisions.
- **Fresh-seed teams/all**: create_all needs `import src.db.models` first; the
  first full build renders `teams/all` empty (Team rows inserted in
  post_process), so the seed appends a partial cycle. Handled by the seed job.
