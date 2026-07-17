# historical-data agent — status

Goal: populate full FRC history (2002–2025, skip 2021) into staging Cloud SQL so
2026 EPA seeds are correct and historical site pages work; then reseed 2026,
backfill hist/ blobs, verify live, and write the maintainer deliverable.

## Environment
- Staging Cloud SQL Postgres reached via **cloud-sql-proxy** (v2.14.1) on
  `127.0.0.1:5433` → `statbotics-staging:us-central1:statbotics-staging-db`
  (binary + log under the session scratchpad). ADC = chondl@gmail.com (owner).
- Backend code: `.worktrees/staging/backend` (branch `staging`), run with the rig
  venv python `.worktrees/rig/backend/.venv/bin/python` (has psycopg2 2.9.10,
  sqlalchemy 2.0.40, google-cloud-storage). `PYTHONPATH` + CWD = staging backend.
- Secrets from `/Users/chondl/statbotics_staging_secret.txt` (never printed):
  `DATABASE_URL=postgresql+psycopg2://<user>:<pwd>@127.0.0.1:5433/<db>`. TBA key
  from `/Users/chondl/thebluealliance_api_key.txt` → `TBA_AUTH_KEY` env.

## Pre-build DB state (verified)
Only 2026 present: years=1, teams=3724, team_years=3724 (2026), events=215,
team_events=8304, matches=18372. Every 2026 team_year had `epa_start=23.74`
(rookie seed) — the bug this backfill fixes.

## Before 2026 EPA spot-checks (rookie-seeded)
| team | epa | norm_epa | total_epa_rank | epa_start |
|------|-----|----------|----------------|-----------|
| 254  | 328.00 | 1946 | 3   | 23.74 |
| 1678 | 280.93 | 1870 | 14  | 23.74 |
| 1323 | 307.56 | 1913 | 5   | 23.74 |
| 118  | 182.09 | 1712 | 140 | 23.74 |
| 4414 | 357.74 | 1994 | 2   | 23.74 |
| 2910 | 280.14 | 1869 | 15  | 23.74 |
| 5413 (mid-pack) | 39.53 | 1482 | 1801 | — |

## BUG FOUND + FIXED (Postgres int32 overflow on historical timestamps)
First build run died on 2006 matches insert:
`psycopg2.errors.NumericValueOutOfRange: integer out of range`. TBA returns a
~1900 placeholder Unix timestamp (`-2208988800`) for matches/events with unknown
time; that underflows Postgres `INTEGER` (int32 min `-2147483648`). CockroachDB
INT is 64-bit, so prod never hit this — a latent Postgres-compat bug exposed only
by pre-modern historical data. Four timestamp columns were `Integer`; fixed to
`BigInteger` (also future-proofs past the 2038 int32 limit):
- `backend/src/db/models/match.py:38` `time`, `:39` `predicted_time`
- `backend/src/db/models/event.py:26` `time`
- `backend/src/db/models/team_event.py:30` `time`
Verified all four compile to `BIGINT` on the postgres dialect. clean_db()
drop+create picks up the new types on the rebuild. (Edit is in the staging
worktree working tree; not yet committed — see final report.)

## Step 1 RESULT — COMPLETE (reset_all_years, 5922s / ~99 min)
24 years loaded (2002–2026, skip 2021). Row counts:
teams=8197, team_years=55962, events=2621, team_events=106880, matches=229136.
Per-year Write DB grew with data size (2002 ~11s → 2016/2017 ~5 min on the
db-f1-micro). TBA: cold cache, all past years fetched + pickled to
`backend/cache/` (~2000+ pickle files); 2026 fetched fresh (cache=False).

### 2026 EPA shift (before rookie-seeded → after history-seeded)
`epa_start` is the real fix; current `epa`/`norm_epa` barely move because
late-2026 EPAs have already converged to on-field performance.
| team | epa before→after | norm_epa | epa_start before→after |
|------|------------------|----------|------------------------|
| 254  | 328.00 → 327.82 | 1946→1944 | 23.74 → **113.95** |
| 1678 | 280.93 → 279.67 | 1870→1867 | 23.74 → **131.80** |
| 1323 | 307.56 → 309.96 | 1913→1915 | 23.74 → **136.19** |
| 118  | 182.09 → 182.35 | 1712→1712 | 23.74 → **117.40** |
| 4414 | 357.74 → 356.94 | 1994→1990 | 23.74 → **113.68** |
| 5413 (mid) | 39.53 → 37.66 | 1482→1481 | — → **33.49** |
distinct `epa_start` values in 2026: **2004** (was 1 — all 23.74).

## Progress
- [x] Step 1: reset_all_years 2002–2026 into Cloud SQL (GCS disabled). DONE.
- [x] Step 2: 2026 reseed (partial=False) + publish. DONE (615s). Manifest gen
      1783695144484876 → 1783704508619062 (cycle 2026-07-10T17:27:05). Published
      `team_years/2026` blob serves 254 `epa.stats.start=113.95` (was 23.74);
      3728 `team/{num}` current-year blobs republished.
      **GCS gotcha found+fixed:** local ADC's default quota_project is the user's
      own project (`poised-kiln-484806-k1`), whose billing is disabled → GCS
      writes 403 "billing account ... disabled". Fixed with env
      `GOOGLE_CLOUD_QUOTA_PROJECT=statbotics-staging` (bills to the enabled
      project). Cloud Run is unaffected (bills to its own project). No code change.
- [x] Step 3: hist/ blob backfill. DONE — 23 past years (2002-2025, skip 2021)
      all verified (jobs==expected total per year); manifest hist_epoch=1.
      Resume tested via interruption at 2005 (checkpoint recorded only completed
      years; restart skipped them). Idempotence: shipped `backfill_blobs.py`
      (no args) re-run wrote 0. Uploads parallelized x32 (sequential per-blob
      exists()+upload ~233 blobs/min → ~3.7h projected; threading finished in
      minutes). Two verification false-positives (both legitimate summary
      filters, not data bugs): `_read_events` drops INVALID events;
      `_read_team_years` drops count==0 team_years. See the per-year log below.
- [x] Step 4: verification. DONE.
  - Smoke suite 9/9 vs staging run.app (teams/all now 8197, was 3724).
  - Live frontend 200 + Next shell: /teams?year=2015, /team/254,
    /event/2015casj, /teams?year=2026, /event/2026tuis.
  - Data live via blob domain: hist/1/team_years/2015 (507 teams),
    hist/1/event/2015casj (57 team_events/111 matches), hist/1/team/254/2015
    (norm_epa **2004.0, rank #1 of 2873** — 254 won 2015 Recycle Rush).
- [x] Step 5: deliverable — docs/superpowers/deliverables/historical-backfill.md.

### Step 3 hist/ backfill — per-year verification (2026-07-10T17:43:24+00:00)
- resume: progress.json completed_years = [2002, 2003, 2004] (epoch 1)
- 2002: SKIP (already complete on resume) OK
- 2003: SKIP (already complete on resume) OK
- 2004: SKIP (already complete on resume) OK
- 2005: wrote 639 (partial-resume; full exp 783) — payload 747 team_years / 25 events (exp 747/34) — FAIL
- 2005: CORRECTION — prior FAIL was a verification false-positive (events *summary* excludes INVALID-status events; 25 of 34). Data correct: 747/747 team_years, all 34 event/{key} blobs present, force re-run wrote 0. OK.

### Step 3 hist/ backfill — per-year verification (2026-07-10T17:49:37+00:00)
- resume: progress.json completed_years = [2002, 2003, 2004, 2005] (epoch 1)
- 2002: SKIP (already complete on resume) OK
- 2003: SKIP (already complete on resume) OK
- 2004: SKIP (already complete on resume) OK
- 2005: SKIP (already complete on resume) OK

### Step 3 hist/ backfill — PARALLEL x32 (2026-07-10T17:54:03+00:00)
- resume: completed_years = [2002, 2003, 2004, 2005] (epoch 1)
- 2002: SKIP (already complete on resume) OK
- 2003: SKIP (already complete on resume) OK
- 2004: SKIP (already complete on resume) OK
- 2005: SKIP (already complete on resume) OK
- 2006: wrote 425/1166 jobs (partial-resume) — payload 1125 ty / 38 ev (exp ty=1125, ev<=39, jobs=1166) — OK
- 2007: wrote 1313/1313 jobs (fresh) — payload 1270 ty / 41 ev (exp ty=1270, ev<=41, jobs=1313) — OK
- 2008: wrote 1546/1546 jobs (fresh) — payload 1498 ty / 46 ev (exp ty=1498, ev<=46, jobs=1546) — OK
- 2009: wrote 1730/1730 jobs (fresh) — payload 1675 ty / 53 ev (exp ty=1675, ev<=53, jobs=1730) — OK
- 2010: wrote 1857/1857 jobs (fresh) — payload 1799 ty / 56 ev (exp ty=1799, ev<=56, jobs=1857) — OK
- 2011: wrote 2118/2118 jobs (fresh) — payload 2053 ty / 63 ev (exp ty=2053, ev<=63, jobs=2118) — OK
- 2012: wrote 2408/2408 jobs (fresh) — payload 2332 ty / 74 ev (exp ty=2332, ev<=74, jobs=2408) — OK
- 2013: wrote 2593/2593 jobs (fresh) — payload 2509 ty / 82 ev (exp ty=2509, ev<=82, jobs=2593) — OK
- 2014: wrote 2802/2802 jobs (fresh) — payload 2696 ty / 103 ev (exp ty=2697, ev<=103, jobs=2802) — FAIL
- 2014: CORRECTION — prior FAIL was a verification false-positive (team_years *summary* drops count==0 teams; 2696 of 2697). Full blob set built (jobs=2802=total, all uploaded). OK. Relaxed verifier: jobs==total authoritative, payload counts are sanity bounds.

### Step 3 hist/ backfill — PARALLEL x32 (2026-07-10T17:57:29+00:00)
- resume: completed_years = [2002, 2003, 2004, 2005, 2006, 2007, 2008, 2009, 2010, 2011, 2012, 2013, 2014] (epoch 1)
- 2002: SKIP (already complete on resume) OK
- 2003: SKIP (already complete on resume) OK
- 2004: SKIP (already complete on resume) OK
- 2005: SKIP (already complete on resume) OK
- 2006: SKIP (already complete on resume) OK
- 2007: SKIP (already complete on resume) OK
- 2008: SKIP (already complete on resume) OK
- 2009: SKIP (already complete on resume) OK
- 2010: SKIP (already complete on resume) OK
- 2011: SKIP (already complete on resume) OK
- 2012: SKIP (already complete on resume) OK
- 2013: SKIP (already complete on resume) OK
- 2014: SKIP (already complete on resume) OK
- 2015: wrote 2993/2993 jobs (fresh) — payload 507 ty / 118 ev (exp ty=2873, ev<=118, jobs=2993) — OK
- 2016: wrote 3251/3251 jobs (fresh) — payload 3114 ty / 135 ev (exp ty=3114, ev<=135, jobs=3251) — OK
- 2017: wrote 3497/3497 jobs (fresh) — payload 3331 ty / 164 ev (exp ty=3331, ev<=164, jobs=3497) — OK
- NOTE 2015: only 507/2873 team_years have count>0 → year *summary* lists 507. Not a bug: all 9894 2015 quals have winner=None (Recycle Rush, no head-to-head quals) — inherent unmodified-model behavior, same as prod. All 2873 per-team 2015 blobs built, so team pages work; /teams?year=2015 summary shows the elim-competing teams.
- 2018: wrote 3797/3797 jobs (fresh) — payload 3617 ty / 178 ev (exp ty=3617, ev<=178, jobs=3797) — OK
- 2019: wrote 3955/3955 jobs (fresh) — payload 3760 ty / 193 ev (exp ty=3760, ev<=193, jobs=3955) — OK
- 2020: wrote 2189/2189 jobs (fresh) — payload 2001 ty / 52 ev (exp ty=2001, ev<=186, jobs=2189) — OK
- 2022: wrote 3247/3247 jobs (fresh) — payload 3062 ty / 183 ev (exp ty=3062, ev<=183, jobs=3247) — OK
- 2023: wrote 3477/3477 jobs (fresh) — payload 3290 ty / 184 ev (exp ty=3290, ev<=185, jobs=3477) — OK
- 2024: wrote 3669/3669 jobs (fresh) — payload 3477 ty / 190 ev (exp ty=3477, ev<=190, jobs=3669) — OK
- 2025: wrote 3895/3895 jobs (fresh) — payload 3690 ty / 203 ev (exp ty=3690, ev<=203, jobs=3895) — OK
- Step 3 DONE: all years verified, manifest hist_epoch=1
