# Statbotics staging mirror — deploy & operate

The one doc for building, deploying, and rebuilding data on the staging mirror.
**The deterministic steps live in the [Makefile](Makefile)** beside this file —
prefer `make <target>` over ad-hoc `gcloud`. Full-history rebuilds are in
[historical-backfill.md](../../deliverables/historical-backfill.md).

Mirror: <https://statbotics.iterativerefinement.com> · API: <https://api-statbotics.iterativerefinement.com>
GCP project `statbotics-staging` · Cloud Run services `statbotics-api` / `statbotics-web` ·
Cloud SQL Postgres `statbotics-staging-db` · GCS bucket `statbotics-staging-site` ·
Cloud Scheduler `statbotics-update` (hourly) + `statbotics-gc` (daily).
Serving is DuckDB-over-Parquet (`API_BACKEND=duckdb`); the DB is still written each
cycle and is the fallback. Cloudflare Workers front both services + the blob bucket.

---

## 1. Standard workflow (autonomous)

Build the feature, deploy it, and verify it in production **without pausing for
permission** — that is the expected loop for this mirror. The agent owns
correctness: adequately test in production before calling it done.

```
make ship        # build both images + roll both services (image-only, preserves config)
```

`make ship` = `build` (Cloud Build → Artifact Registry) + `deploy` (image-only
`gcloud run services update`, which **preserves** each service's env, secrets,
Cloud SQL binding, CPU/memory). Never hand-set env vars on a redeploy unless you
mean to change them — the image-only update is deliberate.

Prereqs: `gcloud` auth (chondl@gmail.com), ADC present, `~/thebluealliance_api_key.txt`,
`~/statbotics_staging_secret.txt`, `~/iterativerefinement_secret.txt` (Cloudflare).

## 2. MANDATORY pre-deploy checklist

Before every deploy, create this as a todo list and work through it — do not
skip items, record the evidence:

- [ ] **On the deploy branch.** Working from `staging`, change merged in and pushed.
- [ ] **Backend tests green.** `cd backend && poetry run python -m pytest tests/ -q`
      (install `httpx==0.24.1` if TestClient errors — starlette 0.27 needs httpx <0.28).
- [ ] **Frontend type-checks.** `yarn build` prints "Compiled successfully". (The
      static-prerender step errors on every page in a sandbox — not a code defect;
      confirm by diffing behavior against unmodified master.)
- [ ] **Reviewed.** Task/whole-branch review clean, or findings triaged and fixed.
- [ ] **Images rebuilt from the deploy branch.** `make build` → both Cloud Builds
      report SUCCESS. Stale images silently ship old code.
- [ ] **Deployed.** `make deploy` → both revisions serve 100% traffic.
- [ ] **Code-live smoke.** Hit the new endpoint/behavior on the deployed service
      (`make verify-event EVENT=<key>`, or curl the new route) — confirm it
      *responds*, not just that the revision rolled.
- [ ] **Data rebuilt if needed** (§3) and the affected data appears.
- [ ] **Production verification.** Exercise the feature end-to-end against the
      mirror (API + a real browser load where relevant). Capture outputs. Check
      unrelated pages for regressions.
- [ ] **`make smoke`** passes.
- [ ] **Schedulers resumed** if paused.

## 3. Data rebuild flows

Deploying code does **not** re-ingest data. Choose the smallest rebuild:

| Situation | Command | Why |
|---|---|---|
| New/changed **current-year** events (e.g. offseason events on TBA) | `make reprocess-curr-year` | Full current-year recompute, fresh TBA fetch (no etag gating). ~4–6 min. |
| One **historical** year changed (e.g. re-ingest 2025 with offseason support) | `make reprocess-year YEAR=2025` | Cloud Run job: `clear_year` + `process_year(partial=False)` + Parquet + hist blobs. Targeted; avoids a 99-min full rebuild. |
| Full history from scratch | [historical-backfill.md](../../deliverables/historical-backfill.md) | `reset_all_years` + reseed + `backfill_blobs`. Long; only for a fresh DB. |

The hourly cron **does** pick up genuinely new TBA events on its own (the
`{year}/events` etag changes). The one gap is a *post-deploy backfill*: events
already on TBA **before** you shipped new ingest code have unchanged etags, so the
cron's pre-check sees nothing new — run `make reprocess-curr-year` once to force a
fresh full ingest. How and when refresh happens is documented in full in
[DATA-REFRESH.md](../DATA-REFRESH.md).

Pause schedulers around a manual rebuild so their manifest writes cannot race the
rebuild's (`reprocess-year` does this automatically):

```
make pause-cron && make reprocess-curr-year && make resume-cron
```

## 4. Offseason & freshness (feature reference)

How offseason events are ingested, why their EPA is frozen, and how the
read-triggered ping + hourly cron keep pages fresh are documented in
**[DATA-REFRESH.md](../DATA-REFRESH.md)** — the authoritative reference for how and
when the mirror recomputes EPA. Deploy-relevant summary:

- Offseason events (TBA type 99, year ≥ 2025) ingest as `EventType.OFFSEASON`,
  `week = 9`, with quality filters; their matches **never update EPA**.
  `EVENT_TYPE_OVERRIDES` (e.g. `2026isrtp` → DISTRICT) is honored and DOES.
- Freshness comes from a fire-and-forget ping on live event pages (≤ every 5 min
  while viewed) plus the hourly cron backstop — both funnel into
  `/v3/site/update_curr_year`. Nothing to configure per deploy.
- **Events list** buckets by date window, not status: any non-completed event
  with `start_date <= today` is *Ongoing* (even before its schedule posts —
  normal on offseason load-in days), future events are *Upcoming*, finished
  events are *Completed*. Prevents started-but-unscheduled events from vanishing.

## 5. Post-deploy gotchas

- **Edge 524 on long ETL triggers.** Cloudflare times out at ~100 s; Cloud Run
  keeps running (timeout 3600 s). A 524 from `reprocess-curr-year` is expected —
  poll `make logs | grep -E "Write DB|Write Storage"` for completion.
- **DuckDB sync TTL ~30 s.** A freshly published event can 404/500 for up to
  ~30 s after a write while the read layer re-syncs Parquet from the manifest.
- **Cold TBA cache.** A fresh container (new revision or job) has an empty TBA
  pickle cache: first full-year fetch ~2 min; full history ~99 min. Re-runs in
  the same container are fast.
- **8Gi / 2 vCPU floor.** The serving container runs the full EPA replay +
  snapshot + Parquet serialization; it OOMs below 8Gi (needs ≥2 vCPU). Measured:
  2Gi OOM @2055MiB, 4Gi OOM @4166MiB. Don't shrink it.
- **Jobs vs services for heavy work.** Cloud Run *services* cap at 3600 s;
  *jobs* allow far longer. Full-history / single-year rebuilds run as jobs
  (`reprocess-year`, backfills), never as an HTTP call to the service.

---

## 6. Architecture (cloud-agnostic)

Two-tier app + a static blob layer + a periodic trigger:

```
            (CDN / DNS + TLS)
                   │
      ┌────────────┴─────────────┐
  Frontend                   Backend API
 (Next.js 13,              (FastAPI, one
  `next start`)             container; all routes)
      │  reads blobs first,      │  reads/writes
      │  API fallback            ▼
      ▼                      Relational DB
  Blob store ◄─── writes ───  (PostgreSQL)
 (public-read,   (manifest +      ▲
  S3-compatible)  versioned       │ hourly cron GET /v3/site/update_curr_year
                  blobs)     (scheduler)
```

Component **contracts** — swap any implementation that satisfies these:

1. **Backend container.** FastAPI via `gunicorn -k uvicorn.workers.UvicornWorker
   main:app` on `$PORT`. Serves `/v3` (REST), `/v3/site` (frontend-shaped +
   writes blobs), `/v3/data/*` (ETL triggers). Needs TBA egress, **≥2 GiB RAM**
   (8Gi in practice). Env: `PROD=True`, `DATABASE_URL` (SQLAlchemy Postgres URL;
   `+psycopg2` + `PGPASSWORD` keeps the password out of the URL; dialect must
   resolve to `postgresql`), `GCS_BUCKET`, `TBA_AUTH_KEY`, `API_BACKEND=duckdb`.
2. **Relational DB.** PostgreSQL 15. Schema via `Base.metadata.create_all` (7
   tables). Single-writer ETL, smallest tier is fine.
3. **Blob store.** Public-read, S3-compatible. Backend writes with
   `google-cloud-storage`: a short-TTL `manifest.json` written **last** each
   cycle, immutable `v2/{logical}.{hash}` blobs, legacy unversioned paths, and
   `hist/{epoch}/{logical}` backfill blobs. Readers resolve logical paths through
   the manifest. Needs CORS for the frontend origin.
4. **Frontend container.** Next.js 13 `next build` → `next start` (server mode,
   **not** `next export`). `BACKEND_URL` + `BUCKET_URL` are **inlined at build
   time** (Next `env` block) — set as build args, not runtime env. Needs
   `NODE_OPTIONS=--max_old_space_size=3072` at build.
5. **Scheduler.** Hourly `GET /v3/site/update_curr_year` (cheap TBA etag
   pre-check, then a backgrounded partial update).
6. **CDN / DNS / TLS.** `statbotics.<domain>` → frontend, `api-statbotics.<domain>`
   → backend, terminating TLS.

Data contract: a blob payload is **byte-identical** to the corresponding site-API
response for the same data, and EPA values are consistent across event page, team
page, list, and API after each cycle.

## 7. GCP realization (what `deploy.sh` does)

| Contract | GCP service | Notes |
|----------|-------------|-------|
| Backend container | **Cloud Run** `statbotics-api` | `--allow-unauthenticated`, min 0 / max 2, cpu 2 / mem 8Gi, timeout 3600, Cloud SQL via `--add-cloudsql-instances` (unix socket `/cloudsql/CONN`) |
| Relational DB | **Cloud SQL** Postgres 15 `db-f1-micro` | ZONAL, 10 GB, public IP; `DATABASE_URL=postgresql+psycopg2://USER@/DB?host=/cloudsql/CONN`, `PGPASSWORD` from Secret Manager |
| Blob store | **GCS** bucket `statbotics-staging-site` | uniform access, `allUsers:objectViewer`, CORS for the frontend origin |
| Schema + seed | **Cloud Run job** `statbotics-seed` | backend image; `create_all` + `update_curr_year(partial=False)` + `refresh_teams` + partial |
| Frontend container | **Cloud Run** `statbotics-web` | image built with `--build-arg BACKEND_URL=…/v3/site BUCKET_URL=https://$BLOB_DOMAIN PROD=True` |
| Scheduler | **Cloud Scheduler** `statbotics-update` | `0 * * * *` → backend `/v3/site/update_curr_year` |
| CDN/DNS/TLS | **Cloudflare** Workers `statbotics-proxy` (+ `statbotics-blob-proxy`) | Worker rewrites Host to the `*.run.app` origin; edge TLS. run.app hosts are stable across revisions, so redeploys need no DNS change. |
| Secrets | **Secret Manager** `tba-auth-key`, `db-password` | compute SA granted `secretAccessor` |
| Images | **Artifact Registry** repo `statbotics` | built by Cloud Build |

First-time stand-up order (`deploy.sh all`): `apis → sql → bucket → secrets →
images → backend → seed → frontend → scheduler → dns`. Cloud SQL creation is the
long pole (~5–10 min). The backend Dockerfile swaps source `psycopg2` →
`psycopg2-binary` (slim image can't compile the source pin).

**Why a Worker instead of a CNAME:** a proxied CNAME to a `run.app` host forwards
the custom Host, which Cloud Run 404s (it routes by the `*.run.app` host). The
Worker sets the URL host to the run.app origin so the subrequest carries the
right Host.

## 8. Porting notes — AWS equivalents

| GCP | AWS | Porting surface |
|-----|-----|-----------------|
| Cloud Run (service) | **App Runner** / **ECS Fargate** | Same container; set `PORT`, env, VPC connector to RDS. |
| Cloud Run job (seed) | **ECS RunTask** / CodeBuild | Same image + command override. |
| Cloud SQL Postgres | **RDS PostgreSQL** (db.t4g.micro) | `DATABASE_URL=postgresql+psycopg2://USER:PWD@HOST:5432/DB` (host+port over VPC, `sslmode=require`). Dialect stays `postgresql`. |
| GCS + public read | **S3** + **CloudFront** | Main surface: backend writes via `google-cloud-storage` (`src/google/storage.py`). Either run a GCS→S3 shim (client honors `STORAGE_EMULATOR_HOST`, as the local rig does against fake-gcs-server) or swap the ~5 call sites to `boto3`. Manifest/versioning logic in `publish.py` is pure/portable. |
| Cloud Scheduler | **EventBridge Scheduler** | cron `0 * * * *` → HTTP target. |
| Secret Manager | **Secrets Manager** / SSM | Inject as container env. |
| Artifact Registry | **ECR** | `docker push`. |
| Cloud Build | **CodeBuild** / local docker build | Frontend still needs the `BACKEND_URL`/`BUCKET_URL` build-args. |
| Cloudflare Worker | **CloudFront + ACM** (or keep Cloudflare) | Distribution with the App Runner/ALB origin + ACM cert. |

## 9. Inputs (secret files, never committed)

- `~/thebluealliance_api_key.txt` — `X-TBA-Auth-Key=<value>`.
- `~/statbotics_staging_secret.txt` — DB password etc. (generated by `deploy.sh sql`), chmod 600.
- `~/iterativerefinement_secret.txt` — `CLOUDFLARE_API_TOKEN=…`, `CLOUDFLARE_ACCOUNT_ID=…`.

All account-specific values are variables at the top of `deploy.sh` and the
Makefile; override via env to target a different project/domain.
