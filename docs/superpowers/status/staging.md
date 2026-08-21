# Staging deployment — statbotics

STATUS: LIVE (verified 2026-07-10)

Live staging instance of statbotics with the workstream's fixes, on GCP project
`statbotics-staging`, fronted by Cloudflare on the `iterativerefinement.com` zone.

## Live URLs

| What | URL |
|------|-----|
| Frontend | https://statbotics.iterativerefinement.com |
| Backend API | https://api-statbotics.iterativerefinement.com  (e.g. `/v3/team/254`, `/info`) |
| Public bucket | https://storage.googleapis.com/statbotics-staging-site  (manifest at `/manifest.json`) |
| Blob CDN | https://blobs-statbotics.iterativerefinement.com  (Cloudflare-proxied bucket; frontend `BUCKET_URL`; see `cf-blob-proxy.md`) |
| Backend run.app | https://statbotics-api-630091002690.us-central1.run.app |
| Frontend run.app | https://statbotics-web-630091002690.us-central1.run.app |

All `gcloud` commands below use `--project=statbotics-staging`. Secrets are
provided as environment variables from the operator's Keychain-backed
environment, plus GCP Secret Manager — never printed here or in any tracked
file.

Reproducible redeploy: `docs/superpowers/rig/deploy/deploy.sh` (parameterized) +
`docs/superpowers/rig/deploy/DEPLOY.md` (architecture, cloud-agnostic + GCP + AWS
porting notes).

---

## Integration branch `staging`

Worktree `/Users/chondl/learn/statbotics/.worktrees/staging` (branch `staging`).
Pushed to `fork` only.

### 2026-07-10 — REBUILT for the full migration stack (`staging` head now `eebb3e8`)

The review-fixes agent force-pushed (rewrote) every stack + track branch. The old
`staging` (`67686cb`) had merged the *pre-rewrite* versions, which conflict badly
with the rewritten history — so `staging` was **rebuilt clean** rather than merged
forward, then **force-pushed** (coordinated, single-agent; `staging` is ours only;
logged in COORDINATION.md). New lineage:

- `staging-v2` (`8ad6741`, pushed to fork) = `fork/db-retirement` **stack tip**
  (`f94a84c` — contains the whole stack: bucket-first-serving → blob-gc →
  state-snapshot → duckdb-api → db-retirement) then merged in order:
  `postgres-compat`, `epa-consistency`, `match-page-fixes`, `qa-fixes`,
  `sos-sim-fix`, then the 5 staging-config commits cherry-picked
  (`GCS_BUCKET`/frontend `BACKEND_URL`+`BUCKET_URL` env, backend+frontend
  Dockerfiles, `psycopg2-binary`, `/docs/rest` iframe).
- `staging` = `staging-v2` + 2 ops commits: `backfill_parquet.py` (DB→Parquet
  historical backfill tool) and the single-manifest-write fix for it. **Head
  `eebb3e8`.**

Conflict resolutions: `epa-consistency` vs the stack in `storage.py`/`data/main.py`
resolved in favor of the **stack's manifest/content-hash publish** (its blob-gate
is superseded); epa-consistency's DB honest-diff gate (`data/utils.py` nan_safe_eq),
breakdown deferral (`read_tba.py`), CUTOFF=200, F5 blank-score, and NaN-stable
event gate all survive. `qa-fixes` vs the stack in `storage.tsx` resolved onto the
stack's manifest-aware/A7 version (kept `bucketInFlight`+`fetchBucketData` dedup,
manifest retry-on-null, `toLogicalPath` regex; layered qa-fixes' `inFlight`/
`fetchAndStore` query dedup on top). Verified before touching staging: compile OK,
flake8 clean on changed files, rig full-stack cycle (Read/Write Snapshot + Write
Storage w/ Parquet fold + Write DB) smoke 9/9, DuckDB-over-Parquet point + list
serving confirmed on the freshly-written Parquet.

(Superseded history for reference — the OLD pre-rewrite `staging` **`34e6755`→
`67686cb`** merge log is preserved below under "Superseded (pre-rebuild) merge
SHAs".)

### Superseded (pre-rebuild) merge SHAs (OLD `staging` — kept for reference only)

The block below documents the OLD `staging` that was force-replaced on 2026-07-10.
It merged the pre-rewrite branch versions and is no longer the deployed lineage.

#### Final merge SHAs (from the reworked/squashed track heads)

| Branch | Head merged | Merge commit |
|--------|-------------|--------------|
| postgres-compat | `f8d8026` | (fast-forward onto master) |
| epa-consistency | `146b763` (commits `bbcc2b1`, `06f8614`, `3050e1f`, `146b763`) | `91b0a5a` |
| bucket-first-serving | `e7f8a83` (commits `eabcdec`, `202d21e`, `f2f09cd`, `e7f8a83`) | `11aadea` |

storage.py conflict resolution: kept Track 2's `add()` + manifest/content-hash
publish for `event/{key}`; it supersedes Track 1's per-event render-diff gate
(content hashing achieves the same "publish only when the rendered payload
changed"). Track 1's honest DB write gate (`src/data/utils.py`,
`src/db/models/main.py`, `src/db/write/template.py`), CUTOFF=250 batch size, and
breakdown deferral (`src/tba/read_tba.py`) live in separate files and survived
the merge intact.

### Staging-config commits (on top of the merges)

- `5bc08aa` — `GCS_BUCKET` env override in `backend/src/google/storage.py`
  (bucket names are global; cannot reuse `site_v1`/`site_dev_v1`); frontend
  `BACKEND_URL`/`BUCKET_URL` env overrides in `frontend/src/constants.tsx`
  inlined via `frontend/next.config.js`, falling back to prod/local defaults.
- `8044604` — `backend/Dockerfile` + `.dockerignore` (single-service Cloud Run
  image); `TBA_AUTH_KEY` env override in `backend/src/tba/main.py`.
- `e145e25` — `frontend/Dockerfile` + `.dockerignore` (`next start` server mode).
- `f2382b3` — Dockerfile uses `psycopg2-binary` (source `psycopg2==2.9.10` will
  not build in `python:3.11-slim`; identical module, the swap the rig also uses).
  Done at build time via `sed`, keeping tracked `requirements.txt` pristine.
- `34e6755` — merged `match-page-fixes` (draft PR chondl/statbotics#5): fixes the
  infinite TBA media-request loop on `/match/{key}` pages (unstable `teams` array
  in `imageRow.tsx`) and the Prev/Next buttons that changed the URL but not the
  page (stale-data guard in `pages/match/[match_id].tsx`). Frontend-only.
- `ee066d0` — merged `qa-fixes` (draft PR chondl/statbotics#6): not-found delay
  8000ms → 1500ms (`notFound.tsx`), in-flight fetch dedup in `storage.tsx`
  `query()` (conflict resolved onto Track 2's manifest-aware version), and
  `.nullslast()` on all `noteworthy_matches.py` order_by clauses (null/DQ
  placeholder matches no longer rank #1). See
  `docs/superpowers/status/qa-fixes.md`.
- `3774e00` — staging-only: `/docs/rest` iframe derives from `BACKEND_URL`
  instead of hardcoding prod `api.statbotics.io/docs` (currently 500ing). Not
  part of any upstream-bound PR. **Final head: `3774e00`.**

### Redeploys after the initial stand-up

- 2026-07-10 — **Quality + perf fixes deployed** (`staging` `eebb3e8` → `a4a9c8a`;
  net diff = 5 files: `frontend/src/api/{match,team}.tsx`,
  `frontend/src/pagesContent/match/[match_id]/table.tsx`,
  `frontend/src/pagesContent/teams/tabs.tsx`, `backend/src/tba/read_tba.py`).
  Merged `db-retirement` (cascade-rebased to carry 2 new perf commits) +
  `quality-fixes` (off master, draft PR chondl#12). **Before:** api
  `statbotics-api-00009-kt9`, web `statbotics-web-00008-qqk`. **After:** api
  `statbotics-api-00010-ln6`, web `statbotics-web-00009-jv8` (100% traffic each).
  Smoke **9/9**. Live browser verify all PASS.
  - **P0 `/teams?year=2021` crash — FIXED.** BubbleChart `columnOptions` guards
    the RP columns on `RP_NAMES[year]` (undefined for the no-season year 2021)
    instead of `year >= 2016`. Page now renders the graceful "An error occurred,
    please try again later." state like `/events?year=2021`.
  - **P1 pre-2016 match "NaN" — FIXED.** The match breakdown renders "N/A" for a
    NaN alliance-total (pre-component years sum absent component EPAs). Verified
    `/match/2015casj_qm1`: Predicted Auto/Teleop/Endgame/Fouls = N/A, Total 28/89
    real, zero literal "NaN" on the page.
  - **P2 2015 elim winners — FIXED IN CODE (backend); LIVE DATA BACKFILL
    DEFERRED.** `read_tba.get_event_matches` now derives an elim winner from the
    alliance scores when TBA `winning_alliance` is absent, and restricts the
    2015 winner-less rule to quals (`comp_level == "qm"`). Verified on the rig vs
    live TBA (2015new + 2015casj): before → QF 8/8 + SF 6/6 winner-less; after →
    all elim winners derived, quals still winner-less. The deployed api image
    carries the fix, but the staging 2015 event blobs/parquet were built from the
    old code and are **not** rebuilt (see 2015 backfill note below) — the fix is
    proven on the rig and applies to any future historical rebuild.
  - **PERF-1 manifest edge-caching — FIXED (CF worker, live).** `statbotics-blob-
    proxy` now `cacheEverything: true` for `/manifest.json` too and rewrites the
    client-facing `Cache-Control` back to `max-age=60` (the Worker's own response
    header wins over the zone's 4h Browser Cache TTL floor — verified). Manifest:
    `cf-cache-status: HIT`, `age` advancing to ~60 then `REVALIDATED`,
    `max-age=60` preserved; browser conditional GET → 304. Removes the ~160-260ms
    GCS RTT from the head of every cold page. Docs: `cf-blob-proxy.md`,
    `deliverables/cloudflare-bucket-proxy.md`, `deploy.sh` embedded worker.
  - **PERF-2 match pages off the API path — FIXED.** `getMatch` derives the match
    view from the edge-cached `event/{key}` blob (the match key encodes its
    event; the event blob carries the match + its `team_matches` + `team_events`
    — byte-parity with `/v3/site/match/{key}`), API fallback preserved. Live
    `/match/2026cmptx_f1m1`: fetches `blobs.../v2/event/2026cmptx.<hash>`
    (immutable, 200), **zero** `/v3/site/match/` API calls. Data-path
    before/after (2015casj_qm1): Cloud Run `/v3/site/match` = **41s cold-start /
    0.17-0.27s warm**; event blob via edge = **~80-90ms HIT**.
  - **PERF-3 team event wave — FIXED.** `getTeamYear` starts the event-blob wave
    as soon as `team_to_events` resolves instead of gating it on the whole
    metadata `Promise.all` (which included the 663KB `team_years/{year}` blob the
    event wave does not need).
  - Stack: 2 perf commits on `bucket-first-serving` (PR #2), cascade-rebased
    `blob-gc → state-snapshot → duckdb-api → db-retirement`, force-pushed to fork;
    PR #2 body updated with the two changes.

- **2015 elim-winner staging backfill (procedure — DEFERRED, judgment call).** A
  targeted single-year rebuild still recomputes all 2015 team win/loss records
  and event-prediction stats and would mutate the live historical data other
  verification leans on; per the task, documented rather than re-run. To apply
  the winner fix to live 2015 data later: (1) rebuild the 2015 year into the
  staging DB with the current api image so `Match.winner` (and derived records)
  recompute; (2) re-run `backfill_parquet.py` for 2015 so `/v3` serves the
  updated rows; (3) re-run `backfill_blobs.py` for 2015 so `hist/{epoch}/event/
  2015*` blobs carry the corrected winners. Keep the single-terminal-manifest-
  write discipline (no per-year manifest read-modify-write races).

- 2026-07-10 — **FULL MIGRATION STACK deployed** (branch rebuilt to `eebb3e8`, see
  above). Both images rebuilt from the new `staging`. **Before:** api
  `statbotics-api-00006-7fs`, web `statbotics-web-00007-8p2`. **After:** api
  `statbotics-api-00009-kt9`, web `statbotics-web-00008-qqk` (100% traffic each).
  - **Backend env now includes `API_BACKEND=duckdb`** so `/v3` serves from
    DuckDB-over-Parquet (`src/api/backend.py` imports the read fns from
    `src.db_duckdb` under that flag). **DB is kept connected** (no `DISABLE_DB`) —
    it is still written each cycle and is the fallback. Snapshot + Parquet
    publishing are on by default in the code (no flag needed).
  - **Memory: 2Gi → 8Gi, CPU 1 → 2.** The full-stack cycle run *inside* the
    serving container (EPA replay + cycle-start deepcopy + 24 MB snapshot
    serialize + Parquet serialize + DuckDB cache) OOMs at both 2Gi (peak 2055 MiB)
    and 4Gi (peak 4166 MiB); 8Gi clears it (8Gi requires ≥2 vCPU on Cloud Run).
    `deploy.sh` `API_MEMORY`/`API_CPU` updated to 8Gi/2.
  - **Historical Parquet backfill 2002–2026 (24 yrs, skip 2021).** The historical
    DB build ran GCS-disabled, so DuckDB had no historical Parquet. One-off Cloud
    Run job `statbotics-parquet-backfill` (backend image, Cloud SQL socket, bucket
    via SA) ran `backfill_parquet.py`, which reads existing DB rows per year and
    exports them through the reviewed `build_parquet_uploads` path — **no EPA
    recompute, no DB writes.** First run used per-year `write_parquet`
    (read-modify-write of the manifest) and **raced the manifest's 60 s
    Cache-Control** — only 6 non-contiguous years survived; fixed to a **single
    terminal manifest write** (upload all content-addressed blobs, then one
    `write_manifest`). Re-run → all 24 years × 6 tables present. `--verify` pass
    (reads each Parquet back, counts rows vs DB): **144/144 tables match, 0
    mismatch.**
  - **Full update cycle** (`/v3/data/update_curr_year`) succeeded at 8Gi in ~139 s
    (Write Snapshot 33 s, Write Storage+Parquet-fold 44 s, Write DB 14 s): snapshot
    `state/snapshot.2026` (23.26 MiB) written, single-manifest publish advanced the
    cycle stamp (`17:27:05 → 19:33:32`), 2026 Parquet refreshed, and **all 24
    historical Parquet years preserved** in the new manifest (carry-forward). Note
    the scheduler endpoint `/v3/site/update_curr_year` backgrounds the same heavy
    cycle on the serving instance, so it also needs the 8Gi headroom.
  - **Verification:** smoke **9/9** (`/v3/team/254` now DuckDB-served, teams/all
    8197 historical teams, consistency max diff 0.0000). Historical `/v3` served by
    DuckDB-over-Parquet: `/v3/team_year/254/2015`, `/v3/team_year/1690/2018`
    (Orbit epa 200.31), `/v3/team_year/148/2014` (Robowranglers),
    `/v3/team_years?year=2015` (1000-row page cap, team 1002 epa 23.84). Frontend
    rebuilt with `BUCKET_URL=blobs-statbotics…` inlined (verified in
    `_app-*.js`); pages 200 for `/`, `/team/254`, `/event/2026caclv`,
    `/teams?year=2015`, `/match/2026mimar_qm43`; blob domain serves
    `manifest.json` + `teams/all` (cf-cache active). `DB_LESS_SEED_INCOMPLETE:
    false` (DB connected). Schedulers paused for the backfill+deploy window, then
    **resumed** (both ENABLED).
  - **Not re-clicked this session:** interactive in-browser SOS/Simulation tab
    verification (no interactive browser here). The frontend is the freshly-built
    merged `sos-sim-fix` code (clean merge; `worker.ts` + `simulation.tsx`), which
    was validated in-browser on this identical fix in the prior `sos-sim-fix`
    redeploy (below). Event pages hosting those tabs render 200.


- 2026-07-10 — rebuilt + redeployed **statbotics-web only** for the
  `match-page-fixes` merge (`34e6755`). New image digest
  `sha256:b4e446e…`, revision `statbotics-web-00003-42m` (100% traffic). Backend
  untouched. Verified live on
  `https://statbotics.iterativerefinement.com/match/2026mimar_qm43`: steady-state
  TBA `/media/` requests 0 (was ~11.7k/5s); Prev/Next now update the displayed
  match (Qual 43 <-> Qual 44) with arrows re-pointing correctly.
- 2026-07-10 — rebuilt + redeployed **both services** for the `qa-fixes` merge
  (`ee066d0`) + staging-only `/docs/rest` fix (`3774e00`). Backend revision
  `statbotics-api-00005-8pd`, frontend revision `statbotics-web-00004-pkk` (100%
  traffic). Triggered `/v3/data/update_curr_year` (66s, success) to regenerate
  the `noteworthy_matches/2026` blob under the fixed sort. Verified live:
  not-found message ~1.6s after client-side route change (was 8s); team-page
  blob fetches single (13 of 14 unique; only `team_to_events` still x2 via Track
  2's `fetchBucketData`, which bypasses `query()` — pre-existing on staging,
  absent on master); Noteworthy "Highest Clean Scores" 964/956/903 descending
  and "Highest Combined" 1561 descending (placeholder 0-match gone); /docs/rest
  renders staging Swagger UI.
- 2026-07-10 — rebuilt + redeployed **statbotics-web only** to switch the
  build-inlined `BUCKET_URL` to the new Cloudflare-proxied blob hostname
  `https://blobs-statbotics.iterativerefinement.com` (see `cf-blob-proxy.md`).
  Revision `statbotics-web-00005-llf` (100% traffic). No `staging` branch
  commit — the value lives in `deploy.sh` build config; image built from head
  `98a15b1` (blob-gc merge). Verified live in the browser: home + `/team/254`
  + `/event/2026caclv` (incl. SOS/Simulation tabs) render, all blob fetches on
  the new hostname (0 direct GCS), repeat fetches `cf-cache-status: HIT`, no
  CORS/console errors. Backend untouched.
- 2026-07-10 — rebuilt + redeployed **statbotics-api only** for the `blob-gc`
  merge (`98a15b1`; merges `blob-gc` = 2 commits on `bucket-first-serving`: v2/
  GC endpoint + gzip manifest). Backend revision `statbotics-api-00006-7fs` (100%
  traffic); frontend untouched (merge's `frontend/src/api/storage.tsx` conflict
  resolved to staging's deployed version). Full run notes in
  `docs/superpowers/status/blob-gc.md`. Summary: ran `/v3/data/update_curr_year`
  (writes the gzip manifest), then `/v3/data/gc_blobs?grace_hours=0` reclaimed 324
  unreferenced `v2/` objects (26,989,396 bytes); second run idempotent (0). Manifest
  `manifest.json` now stored gzip: 169,138 -> 53,559 bytes (verified via `curl`, GCS
  transcoding serves 53,559 gzip to `Accept-Encoding: gzip`, 169,138 decompressed
  otherwise). New Cloud Scheduler job `statbotics-gc` (daily 04:30 UTC, safe 48h
  grace default) — test-triggered, log line landed, 0 deleted (all remaining objects
  referenced). Smoke 9/9; site + team/254 render.
- 2026-07-10 — merged `sos-sim-fix` (draft PR chondl/statbotics#8) into staging
  (`765cec0`); rebuilt + redeployed **statbotics-web only**, revision
  `statbotics-web-00007-8p2` (100% traffic; interim rev `00006-v8k` was built
  before the cf-blob-proxy cutover and briefly reverted its BUCKET_URL — the
  final image carries both the fix and
  `BUCKET_URL=blobs-statbotics.iterativerefinement.com`). Fixes: SOS tab
  blank/NaN when all pre-event EPAs are identical (zero-variance Gaussian throw
  in `worker.ts`), Simulation tab EPA column always 0 (stale
  `epa.total_points.mean` read). Verified live on 2026sccha + 2026mimar: SOS
  score columns numeric with 0 empty/NaN cells, Reload re-runs (10/10 rows
  change), After Event toggle works, Simulation EPA column shows real values,
  blob fetches on blobs-statbotics, no console errors. Smoke 5/9 — the 4 FAILs
  are DB-backed reads 500ing during historical-data's announced backfill (blob
  reads + liveness PASS). Details: `docs/superpowers/status/sos-sim-fix.md`.
- 2026-07-10 — **historical-data**: full-history load into staging Cloud SQL.
  Ran `reset_all_years` 2002-2026 (skip 2021) directly against Cloud SQL via
  cloud-sql-proxy (DB now 24 years: 8,197 teams, 55,962 team_years, 2,621
  events, 106,880 team_events, 229,136 matches). 2026 reseeded
  (`update_curr_year(partial=False)`) + published: `epa_start` corrected from the
  uniform rookie seed (23.74) to history-based (254→113.95); manifest advanced.
  hist/ blobs backfilled for all 23 past years (`backfill_blobs.py`,
  hist_epoch=1). Found+fixed a latent Postgres int32 overflow on match/event
  timestamps (BigInteger) — committed to postgres-compat (`c99681d`) and merged
  into staging (`67686cb`, pushed fork); **no service redeploy** (DB schema
  already bigint, serving unaffected). Smoke 9/9; live historical pages verified.
  Full details: `docs/superpowers/status/historical-data.md`; maintainer guide:
  `docs/superpowers/deliverables/historical-backfill.md`.

---

## Resources created (all in project statbotics-staging)

| Resource | Type | Name | Region / tier / config |
|----------|------|------|------------------------|
| GCS bucket | Standard | `statbotics-staging-site` | us-central1, uniform access, `allUsers` objectViewer (public read), CORS for the staging origin; ~75 MB, ~8.2k objects |
| Artifact Registry | docker repo | `statbotics` | us-central1; holds `statbotics-api`, `statbotics-web` images |
| Secret Manager | secret | `tba-auth-key` | TBA API key (real key from `$TBA_AUTH_KEY`) |
| Cloud Run svc | service | `statbotics-api` | us-central1, image `statbotics-api:latest`, **cpu 2 / mem 8Gi**, min 0 / max 2, `PROD=True`, `GCS_BUCKET`, **`API_BACKEND=duckdb`**, `TBA_AUTH_KEY` from secret, timeout 3600 |
| Cloud Run svc | service | `statbotics-web` | us-central1, image `statbotics-web:latest`, cpu 1 / mem 1Gi, min 0 / max 2, `next start` |
| Cloud Scheduler | http job | `statbotics-update` | us-central1, `0 * * * *`, GET `…/v3/site/update_curr_year`, deadline 1800s |
| Cloud Scheduler | http job | `statbotics-gc` | us-central1, `30 4 * * *`, GET `…/v3/data/gc_blobs` (48h grace default) |
| Cloudflare | AAAA (proxied) | `statbotics`, `api-statbotics`, `blobs-statbotics` | placeholder `100::`, orange-cloud, so the Worker routes intercept |
| Cloudflare | Worker | `statbotics-proxy` | reverse-proxy: rewrites Host to the run.app origin (Cloud Run routes by host); TLS terminated at the Cloudflare edge (valid) |
| Cloudflare | Worker | `statbotics-blob-proxy` | reverse-proxy for the public bucket; edge-caches immutable blobs, manifest uncached (see `cf-blob-proxy.md`) |
| Cloudflare | routes | 3 | `statbotics.…/*`, `api-statbotics.…/*` → `statbotics-proxy`; `blobs-statbotics.…/*` → `statbotics-blob-proxy` |

> **Deleted 2026-07-27 (DB retirement Phase 4):** the `statbotics-staging-db`
> Cloud SQL instance, the `db-password` secret, the `cloudsql.client` IAM grant,
> the `statbotics-seed` job, the `statbotics-parquet-backfill` job, and two other
> stale DB-era jobs. The api service no longer has a Cloud SQL socket,
> `DATABASE_URL`, or `PGPASSWORD`. A final `pg_dump` is preserved in the private
> bucket `gs://statbotics-staging-db-final-export/`. Evidence and preconditions:
> [BILLING.md](../rig/BILLING.md#cloud-sql-decommissioned-2026-07-27--0281day).

Compute/service SA (`630091002690-compute@developer.gserviceaccount.com`)
granted: `secretmanager.secretAccessor` on `tba-auth-key`, `storage.objectAdmin`
on the bucket.

APIs enabled: run, cloudbuild, artifactregistry, secretmanager,
cloudscheduler, storage, compute. (`sqladmin` is left enabled but unused.)

---

## DNS / TLS decision

Chose **Cloudflare Worker reverse proxy** over Cloud Run domain mappings — no
Google domain verification needed, and the zone already uses the "proxied AAAA
`100::` + Worker" pattern (existing `clara`/`mcp` records). A plain proxied CNAME
to a `run.app` host does NOT work (Cloud Run routes by Host header; the CDN would
forward the custom host, which Cloud Run 404s), so the Worker rewrites the Host
to the `…-630091002690.us-central1.run.app` origin. Those run.app hostnames are
stable across revisions, so redeploys don't require touching Cloudflare. TLS is
the zone's Cloudflare edge cert (valid; verified via curl).

---

## Verification

Shared smoke suite (`docs/superpowers/rig/smoke/smoke.py`) pointed at the run.app
API URL (Cloudflare's WAF 403s urllib's default User-Agent; the API check must
bypass the CDN — the bucket is public GCS either way):

```
python3 docs/superpowers/rig/smoke/smoke.py \
  --base-url https://statbotics-api-630091002690.us-central1.run.app \
  --data-url https://statbotics-api-630091002690.us-central1.run.app \
  --gcs https://storage.googleapis.com --bucket statbotics-staging-site --year 2026
```

Result: **9/9 pass** (without `--run-update`):

```
[1] liveness           PASS GET /,  PASS GET /info 200
[2] db-backed reads    PASS /v3/team/254 (254=The Cheesy Poofs norm=1946.0)
                       PASS /v3/site/team_years/2026 (3724 team_years)
[3] blob reads         PASS teams/all (3724), PASS team_years/2026 (3724),
                       PASS event/2026tuis (32 team_events)
[4] consistency probe  PASS event blob epa == API epa (max diff 0.0000)
                       PASS team_years blob epa == API epa (max diff 0.0000)
```

With `--run-update`: **9/10** — check 5 FAILS on the smoke client's hardcoded 60 s
`urllib` timeout only. The synchronous full-cycle endpoint
`/v3/data/update_curr_year` takes ~62–76 s here (full-season EPA replay + Track
1's honest-diff DB writes over the Cloud SQL socket — larger writes are expected
per spec §2.2 items C/D), so the client times out even though the cycle
succeeds. The publish mechanism itself is verified: the manifest `cycle` stamp
advances every cycle (observed `05:41:14 → 05:43:28`, HTTP 200). This is a client
timeout, not a server or publish failure. To get a clean 10/10 the smoke suite
would need a longer timeout on the update call (or an async trigger).

Manual checks (all pass):
- `GET https://statbotics.iterativerefinement.com/` → 200; `/team/254` → 200.
- `GET https://api-statbotics.iterativerefinement.com/v3/team/254` → correct.
- Public bucket blob `team/254` (versioned, via manifest) is byte-consistent
  with the site API `/v3/site/team/254` (Track 2 bucket-first serving).
- Seed produced 3724 teams / 3724 team_years / 215 events; manifest references
  3950 logical blobs; bucket holds ~8.2k objects (versioned + legacy + manifest).

---

## Monthly cost estimate (facts; no time estimates)

> **Superseded — this table predates the 2026-07-27 decommission.** The two
> Cloud SQL rows below (~$8.60/mo, then the single largest line) are gone, along
> with the seed job. For measured actuals and the current run rate, use
> [BILLING.md](../rig/BILLING.md), which is the live cost document.

Budget is $25/mo. Everything except the deleted database is near-zero because
Cloud Run runs at min-instances=0 (no idle charge).

| Resource | Basis | Est. $/mo |
|----------|-------|-----------|
| ~~Cloud SQL `db-f1-micro`~~ | ~~~$0.0105/hr shared-core, always-on~~ | ~~~$7.70~~ (deleted) |
| ~~Cloud SQL storage~~ | ~~10 GB HDD @ ~$0.09/GB~~ | ~~~$0.90~~ (deleted) |
| GCS storage | ~75 MB Standard @ $0.020/GB | <$0.01 |
| GCS egress/ops | low staging traffic, browser reads | ~$0–2 |
| Cloud Run (api+web) | min 0; billed only while serving; offseason cadence | ~$0–2 |
| Artifact Registry | ~1–2 GB images @ $0.10/GB | ~$0.20 |
| Secret Manager | 1 secret @ $0.06 + access | ~$0.08 |
| Cloud Scheduler | 2 jobs (3 free/mo) | $0 |
| Cloudflare | existing zone; Workers free tier | $0 |
| **Total (as estimated then)** | | **~$10–13/mo** |

With the database gone there is no always-on component left: every remaining
line is usage-billed or free-tier.

---

## Teardown (exact commands)

```bash
P=statbotics-staging; R=us-central1
# Cloud Run services
gcloud --project=$P run services delete statbotics-api  --region=$R --quiet
gcloud --project=$P run services delete statbotics-web  --region=$R --quiet
# Scheduler
gcloud --project=$P scheduler jobs delete statbotics-update --location=$R --quiet
gcloud --project=$P scheduler jobs delete statbotics-gc     --location=$R --quiet
# GCS buckets (recursive) — the second holds the final pre-decommission DB dump
gcloud --project=$P storage rm --recursive gs://statbotics-staging-site
gcloud --project=$P storage rm --recursive gs://statbotics-staging-db-final-export
# Secrets
gcloud --project=$P secrets delete tba-auth-key --quiet
# Artifact Registry (removes both images)
gcloud --project=$P artifacts repositories delete statbotics --location=$R --quiet
# Cloudflare (needs $CLOUDFLARE_API_TOKEN from the environment; zone id via API):
#   delete Worker scripts `statbotics-proxy` + `statbotics-blob-proxy`, their 3
#   routes, and the 3 AAAA records (statbotics, api-statbotics, blobs-statbotics).
#   See docs/superpowers/rig/deploy/DEPLOY.md.
```

Fastest full teardown: `gcloud projects delete statbotics-staging` (removes every
GCP resource at once), then delete the Cloudflare Worker/routes/DNS.

---

## Notes / deferred

- **Seed ordering gotcha (fixed in the seed job).** `update_curr_year(partial=
  False)` writes GCS blobs *inside* `process_year`, before `post_process` inserts
  the `Team` rows — so on a fresh DB the first `teams/all` renders empty. The seed
  job appends a partial cycle (`update_curr_year(partial=True)`) so `teams/all`
  re-renders with teams present. Also `Base.metadata.create_all` needs the model
  classes imported first (`import src.db.models`) or it creates no tables.
- **GCS default caching on legacy paths.** The unversioned legacy blob paths are
  written with no `Cache-Control`, so GCS applies `public, max-age=3600`. A blob
  fetched publicly while transiently stale can be edge-cached ~1 h. Real readers
  use the versioned immutable manifest paths (always fresh, unique URLs), so this
  only affects direct legacy-path probes; the fresh-bucket re-seed avoids it.
- **Update cycle > 60 s** (see Verification) — smoke check 5 client-timeout only.
- **Track 2 outage-resilience acceptance (3.4.1)** was validated on the local rig
  (API down → pages render from blobs); not re-simulated against live staging to
  avoid deliberately breaking the running service. Blob/API byte-consistency is
  verified here, which is the substantive guarantee.
- No PRs/issues/comments created on any repo; nothing posted externally. `origin`
  never pushed. `staging` pushed to `fork` only.
