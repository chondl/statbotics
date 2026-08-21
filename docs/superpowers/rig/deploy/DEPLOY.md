# Statbotics staging mirror — deploy & operate

The one doc for building, deploying, and rebuilding data on the staging mirror.
**The deterministic steps live in the [Makefile](Makefile)** beside this file —
prefer `make <target>` over ad-hoc `gcloud`. Full-history rebuilds are in
[historical-backfill.md](../../deliverables/historical-backfill.md).

Mirrors: <https://statbotics.iterativerefinement.com> and
<https://statbotics.popcornpenguins.com> — same code, same commit, one shared
backend/bucket; each domain has its own frontend Cloud Run service and its own
Cloudflare account for DNS/Workers (APIs:
<https://api-statbotics.iterativerefinement.com> /
<https://api-statbotics.popcornpenguins.com>, both proxying the same service).
GCP project `statbotics-staging` · Cloud Run services `statbotics-api` /
`statbotics-web` / `statbotics-web-pp` ·
GCS bucket `statbotics-staging-site` ·
Cloud Scheduler `statbotics-update` (hourly) + `statbotics-gc` (daily 04:30 UTC) +
`statbotics-tba-sweep` (daily 05:30 UTC — one historical year's TBA revalidation).

**There is NO database.** DB retirement completed 2026-07-27 (Phase 4): the
Cloud SQL instance, the `db-password` secret, the `cloudsql.client` IAM grant,
and the `statbotics-seed` DB-mode job are deleted. Serving is
DuckDB-over-Parquet (`API_BACKEND=duckdb`) and the pipeline writes **GCS only**
— unconditionally, with no mode flag. The `make flip-db` / `make flip-dbless`
targets are gone, and so is the `DISABLE_DB` flag itself (deleted once it had
exactly one legal value); there are no modes to switch between. Cloudflare
Workers front both services + the blob bucket. Cost effect and the verification
that preceded deletion: [BILLING.md](../BILLING.md).

---

## 1. Standard workflow (autonomous)

Build the feature, deploy it, and verify it in production **without pausing for
permission** — that is the expected loop for this mirror. The agent owns
correctness: adequately test in production before calling it done.

### There is exactly one way to deploy

```
make ship        # build ALL images + roll ALL services (api + one web per mirror)
```

**That is the only sanctioned deploy command.** Not `gcloud builds submit`, not
`gcloud run deploy`, not `make deploy-api` because "only the backend changed".
The component targets (`build`, `build-api`, `build-web`, `build-web-pp`,
`deploy`, `deploy-api`, `deploy-web`, `deploy-web-pp`) are **guarded** and will
refuse to run unless they were reached through `make ship`:

```
$ make deploy-api
ERROR: 'deploy-api' is a PARTIAL deploy step — use 'make ship'.
```

Deliberate partial deploys must opt in and be justified:
`make deploy-api ALLOW_PARTIAL=1`.

**Why this is a rule rather than a preference.** Three failure modes, all of
which have real precedent in this project's history:

1. **Hand-rolled `gcloud run deploy` drops config.** `make deploy` uses an
   *image-only* `gcloud run services update`, which **preserves** each
   service's env vars, secrets, and CPU/memory. A typed-out `gcloud run deploy`
   re-specifies the container and silently loses them.
2. **Partial deploys drift production from branch history.** The invariant this
   whole file defends is *production == branch history, always* (see
   `check-deploy-src`). Half a revision pair breaks it.
3. **"It didn't change, so I'll skip it" is a judgment call that can be wrong.**
   Rebuilding an unchanged image costs a few cents and ~2 minutes. Being wrong
   about what changed costs a stale production service and a confusing debug
   session. The rule removes the judgment call.

If you find yourself reasoning toward a shortcut — because the change is small,
because a build seems redundant, because you already know which service is
affected — that is precisely the situation this rule exists for. Run `make ship`.

Prereqs: `gcloud` auth (chondl@gmail.com), ADC present, and the secrets from
§9 (env-first; Mac home-dir files are only a fallback).

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
- [ ] **Images rebuilt from the deploy branch.** `make build` → all three Cloud
      Builds report SUCCESS. Stale images silently ship old code.
- [ ] **Deployed.** `make deploy` → all three revisions serve 100% traffic.
- [ ] **Code-live smoke.** Hit the new endpoint/behavior on the deployed service
      (`make verify-event EVENT=<key>`, or curl the new route) — confirm it
      *responds*, not just that the revision rolled.
- [ ] **Data rebuilt if needed** (§3) and the affected data appears.
- [ ] **Production verification.** Exercise the feature end-to-end against the
      mirror (API + a real browser load where relevant). Capture outputs. Check
      unrelated pages for regressions.
- [ ] **`make smoke`** passes and **`make verify-web`** returns 200 for both
      mirror domains.
- [ ] **Schedulers resumed** if paused.

## 3. Data rebuild flows

Deploying code does **not** re-ingest data. Choose the smallest rebuild:

| Situation | Command | Why |
|---|---|---|
| New/changed **current-year** events (e.g. offseason events on TBA) | `make reprocess-curr-year` | Full current-year recompute, fresh TBA fetch (no etag gating). ~4–6 min. |
| One **historical** year changed (e.g. re-ingest 2025 with offseason support) | `make reprocess-year YEAR=2025` | Cloud Run job running `reprocess_year()`: `process_year(partial=False)` + Parquet + hist blobs + chained current-year re-render. Targeted; avoids a full rebuild. |
| Full history from scratch | [historical-backfill.md](../../deliverables/historical-backfill.md) | Db-less `reset_all_years`: one pass emits every year's Parquet + hist blobs + the current-year publish. No DB build/reseed. |

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
  poll `make logs | grep "Write Storage"` for completion (the GCS publish is
  the cycle's completion signal; db-less there is no `Write DB` line at all).
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
  `next start`)             container; all routes;
      │  reads blobs first,  │  serves from DuckDB-over-Parquet)
      │  API fallback        │
      ▼                      │ writes (manifest + versioned blobs
  Blob store ◄───────────────┘  + Parquet + snapshot)
 (public-read,        ▲
  S3-compatible)      │ hourly cron GET /v3/site/update_curr_year
                 (scheduler)

  (No relational DB. The blob store is the whole datastore, 2026-07-27 on.)
```

Component **contracts** — swap any implementation that satisfies these:

1. **Backend container.** FastAPI via `gunicorn -k uvicorn.workers.UvicornWorker
   main:app` on `$PORT`. Serves `/v3` (REST), `/v3/site` (frontend-shaped +
   writes blobs), `/v3/data/*` (ETL triggers). Needs TBA egress, **≥2 GiB RAM**
   (8Gi in practice). Env: `PROD=True`, `GCS_BUCKET`, `TBA_AUTH_KEY`,
   `API_BACKEND=duckdb`, `BACKEND_URL` (the service's own public URL — the data
   router calls back through it).
2. **Relational DB — NONE.** Deleted 2026-07-27. The blob store below is the
   entire datastore; there is no second source of truth to keep in sync.
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
| Backend container | **Cloud Run** `statbotics-api` | `--allow-unauthenticated`, min 0 / max 2, cpu 2 / mem 8Gi, timeout 3600. No Cloud SQL binding — removed 2026-07-27. |
| Relational DB | **none** | Deleted 2026-07-27. Final `pg_dump` archived at `gs://statbotics-staging-db-final-export/` (private). |
| Blob store | **GCS** bucket `statbotics-staging-site` | uniform access, `allUsers:objectViewer`, CORS for the frontend origin. **World-readable — never put dumps or private data here.** |
| Seed / stand-up | **Cloud Run job** `statbotics-seed` (created on demand by `deploy.sh seed`) | db-less: `reset_all_years` + `refresh_teams` + a trailing partial cycle. See also [historical-backfill.md](../../deliverables/historical-backfill.md). |
| Frontend containers | **Cloud Run** `statbotics-web` + `statbotics-web-pp` | one per mirror domain, same code: `BACKEND_URL`/`BUCKET_URL` are inlined at `next build`, so each domain's image is built with its own `--build-arg BACKEND_URL=…/v3/site BUCKET_URL=https://$BLOB_DOMAIN PROD=True` |
| Scheduler | **Cloud Scheduler** `statbotics-update` | `0 * * * *` → backend `/v3/site/update_curr_year` |
| CDN/DNS/TLS | **Cloudflare** Workers `statbotics-proxy` (+ `statbotics-blob-proxy`), in **two accounts** — zone `iterativerefinement.com` (`deploy.sh dns`) and zone `popcornpenguins.com` (`deploy.sh dns-pp`) | Worker rewrites Host to the `*.run.app` origin; edge TLS. Each zone's api-/blobs- hostnames proxy the SAME backend service and bucket; only the frontend origin differs. run.app hosts are stable across revisions, so redeploys need no DNS change. |
| Secrets | **Secret Manager** `tba-auth-key` | compute SA granted `secretAccessor`. `db-password` was deleted with Cloud SQL on 2026-07-27. |
| Images | **Artifact Registry** repo `statbotics` | built by Cloud Build |

First-time stand-up order (`deploy.sh all`): `apis → bucket → secrets → images →
backend → seed → frontend → scheduler → dns → dns-pp`. The `sql` step no longer
exists.
The long pole is now `seed`: a db-less `reset_all_years` over 2002–present,
bounded by TBA fetch time from a cold bucket (task timeout 2 h; ~5–8 min against
a warm TBA cache).

**Why a Worker instead of a CNAME:** a proxied CNAME to a `run.app` host forwards
the custom Host, which Cloud Run 404s (it routes by the `*.run.app` host). The
Worker sets the URL host to the run.app origin so the subrequest carries the
right Host.

**Planned migration:** the intent is to eventually make
`statbotics.iterativerefinement.com` a plain redirect to
`statbotics.popcornpenguins.com` (edit the acct-#1 `statbotics-proxy` Worker to
return a 301 for that hostname). Two dependencies survive such a redirect and
must be handled first if the old zone is ever retired outright: the backend's
`BACKEND_URL` env (its ETL self-call goes through
`api-statbotics.iterativerefinement.com`) would need repointing to the
popcornpenguins API domain, and any external API users on the old api-/blobs-
hostnames would break.

## 8. Porting notes — AWS equivalents

| GCP | AWS | Porting surface |
|-----|-----|-----------------|
| Cloud Run (service) | **App Runner** / **ECS Fargate** | Same container; set `PORT`, env, VPC connector to RDS. |
| Cloud Run job (seed) | **ECS RunTask** / CodeBuild | Same image + command override. |
| ~~Cloud SQL Postgres~~ | — | No longer applicable: there is no database to port. |
| GCS + public read | **S3** + **CloudFront** | Main surface: backend writes via `google-cloud-storage` (`src/google/storage.py`). Either run a GCS→S3 shim (client honors `STORAGE_EMULATOR_HOST`, as the local rig does against fake-gcs-server) or swap the ~5 call sites to `boto3`. Manifest/versioning logic in `publish.py` is pure/portable. |
| Cloud Scheduler | **EventBridge Scheduler** | cron `0 * * * *` → HTTP target. |
| Secret Manager | **Secrets Manager** / SSM | Inject as container env. |
| Artifact Registry | **ECR** | `docker push`. |
| Cloud Build | **CodeBuild** / local docker build | Frontend still needs the `BACKEND_URL`/`BUCKET_URL` build-args. |
| Cloudflare Worker | **CloudFront + ACM** (or keep Cloudflare) | Distribution with the App Runner/ALB origin + ACM cert. |

## 9. Secrets (environment variables only, never committed, never printed)

Every secret is read **only from its environment variable**. The canonical
store is the operator's macOS Keychain; the values are injected into the shell
environment at start (on the Mac and in agent containers alike). Home-directory
secret files are fully retired — there is no file fallback anywhere in the
tooling. A missing secret fails fast naming the **variable** and pointing at
the operator's environment.

| Env var | Used by |
|---|---|
| `TBA_AUTH_KEY` | `deploy.sh secrets` (writes it to Secret Manager); rig scripts via `rig_bootstrap.py` |
| `CLOUDFLARE_API_TOKEN` | `deploy.sh dns` (Cloudflare acct #1, zone iterativerefinement.com) |
| `CLOUDFLARE_ACCOUNT_ID` | `deploy.sh dns` |
| `POPCORNPENGUINS_CLOUDFLARE_API_TOKEN` | `deploy.sh dns-pp` (Cloudflare acct #2, zone popcornpenguins.com) |
| `POPCORNPENGUINS_CLOUDFLARE_ACCOUNT_ID` | `deploy.sh dns-pp` |

Routine `make ship` needs **none** of these — only `gcloud` auth. The
Cloudflare tokens are needed only when (re)running the `dns`/`dns-pp` steps,
and `TBA_AUTH_KEY` only when (re)creating the Secret Manager entry.

All account-specific values are variables at the top of `deploy.sh` and the
Makefile; override via env to target a different project/domain.
