# Historical backfill — full-history DB build, 2026 reseed, and hist/ blob export

Audience: a maintainer adopting these changes on a fresh deployment. This
describes how to populate a Statbotics database with the full FRC history
(2002–present), why the current year must be reseeded afterward, and how to
export the historical (`hist/`) site blobs. It is written against the staging
deployment (Cloud SQL Postgres + a GCS bucket) but the steps are generic.

---

## What this produces and why it matters

A fresh deployment that has only the current season is wrong in two ways:

1. **Every team is seeded as a rookie.** Season-start EPA (`epa_start`) is
   regressed from the prior two years. With no history, every team starts at the
   same rookie constant, so current-season EPAs are systematically off for strong
   and weak teams alike.
2. **Historical site pages are empty.** `/teams?year=2015`, historical event
   pages, and a team's multi-year history read from `hist/` blobs (or the DB)
   that do not exist.

The fix is three ordered steps:

1. **Full-history DB build** (`reset_all_years`) — 2002→CURR_YEAR sequentially,
   so each season's EPA seeds are computed from the real prior seasons.
2. **Current-year reseed** (`update_curr_year(partial=False)`) + publish — the
   current-year blobs served to the site are recomputed now that history exists,
   and the corrected values are published to the bucket + manifest.
3. **Historical blob backfill** (`backfill_blobs.py`) — export `hist/` blobs for
   every past year so historical pages render from the bucket.

---

## Prerequisites

- **TBA API key.** A The Blue Alliance read API key. The backend reads it from
  the `TBA_AUTH_KEY` env var (falling back to the hardcoded public key in
  `src/tba/constants.py`). Historical builds make a large number of TBA calls on
  a cold cache (see "TBA caching" below), so use a real key.
- **Database.** A reachable SQL database. The backend connects via `CONN_STR`
  (`src/constants.py`): `DATABASE_URL` overrides everything (e.g.
  `postgresql+psycopg2://USER:PWD@HOST:5432/DB` for Cloud SQL Postgres), else it
  builds a CockroachDB URL from `CRDB_*`/`PROD`. `src/db/transaction.py` selects
  retry behavior from the engine dialect, so both Postgres and CockroachDB work.
  For Cloud SQL from a workstation, run `cloud-sql-proxy <conn-name> --port 5433`
  and point `DATABASE_URL` at `127.0.0.1:5433` (the password can go in the URL or
  in `PGPASSWORD`).
- **Object store (bucket).** A GCS bucket for the site blobs. The backend picks
  the name from `GCS_BUCKET` (falling back to `site_v1`/`site_dev_v1`).
  `storage.Client()` uses Application Default Credentials, so `gcloud auth
  application-default login` (or a service account) with object-write on the
  bucket is required for steps 2 and 3. Step 1 does not need the bucket.
- **Python deps.** The backend's requirements. Note `requirements.txt` pins
  source `psycopg2`; on a machine without libpq/`pg_config`, install
  `psycopg2-binary` instead (identical module).

### REQUIRED code fix for Postgres: 64-bit timestamp columns

On CockroachDB, `INT` is 64-bit, so match/event timestamps never overflow. On
**Postgres**, SQLAlchemy `Integer` maps to `int4` (32-bit), and TBA returns a
placeholder Unix timestamp of `-2208988800` (~1900) for matches/events with an
unknown time. That value underflows int32, and the historical build dies at the
first affected year (2006) with `psycopg2.errors.NumericValueOutOfRange: integer
out of range`. The four timestamp columns must be `BigInteger` (this also
future-proofs real timestamps past the 2038 int32 limit):

- `src/db/models/match.py` — `time`, `predicted_time`
- `src/db/models/event.py` — `time`
- `src/db/models/team_event.py` — `time`

`clean_db()` (drop + create) applies the new column types on the next build. If
your database already has these columns as `integer`, either recreate the schema
(the historical build does this) or `ALTER TABLE ... ALTER COLUMN time TYPE
bigint`.

---

## Step 1 — Full-history DB build (`reset_all_years`)

`reset_all_years()` in `src/data/main.py`:

1. `clean_db()` — **drops and recreates all tables** (destructive: wipes any
   existing current-year rows), so the build is a clean, from-scratch rebuild.
2. `load_teams_tba(cache=True)` — loads the full team list from TBA.
3. For `year` in 2002…CURR_YEAR (skipping **2021** — no season): `process_year`
   fetches TBA data, computes averages/wins/EPA, and writes the year's rows. The
   per-year `all_team_years` accumulator is passed forward so each season's EPA
   seeds are computed from the **real prior seasons** — this is the whole point of
   building in order.
4. `post_process()` — inserts the `Team` rows (teams are created here, not per
   year — see the gotcha below) and finalizes aggregate win records + EPA
   post-processing.

The upstream `/v3/data/reset_all_years` HTTP endpoint may be stubbed
(`return {"status": "skipped"}`); run the function directly instead. Minimal
driver (register models, disable the current-year GCS publish so the long run
never touches the bucket, then call it):

```python
import src.db.models            # register tables on Base.metadata
import src.data.main as dm
dm.DISABLE_GCS = True           # skip the CURR_YEAR blob write during the build
dm.reset_all_years()
```

Run it from the `backend/` directory (so the TBA `cache/` lands there) with
`DATABASE_URL`/`PGPASSWORD`/`TBA_AUTH_KEY`/`PYTHONPATH` set, as a persistent
background process writing to a log file (the run is long; do not tie it to an
interactive session).

### TBA caching behavior

`get_tba()` (`src/tba/main.py`) uses a filesystem pickle cache at
`cache/<url>/data.p` **relative to the working directory**. `reset_all_years`
passes `cache=True` for every past year (`year_num < CURR_YEAR`) and `cache=False`
for the current year. So:

- **Cold cache:** every past-year TBA request is a miss and is fetched + pickled.
  This is the bulk of the wall-clock time and TBA call volume (thousands of
  requests: the team list, plus events and per-event matches for ~23 seasons).
- **Warm cache:** past-year requests are served from the pickle cache (near-zero
  network), so re-runs are fast. The current year is always fetched fresh.

### Resumability

`reset_all_years` is **not** checkpointed mid-run — it always starts from
`clean_db()` and rebuilds every year. But because the TBA pickle cache persists
across runs, **resuming after a failure = re-running the same command**: past
years that were already fetched are cache hits, so the re-run skips the slow
network phase and only recomputes + rewrites. (This is how the Postgres
timestamp fix above was recovered: the first run cached 2002–2006 before dying at
the 2006 insert; after the model fix, the re-run replayed those years from cache
in seconds.)

### Expected data volumes (per year, approximate)

Row counts grow with the sport. Per-year `team_years` range from ~640 (2002) to
~3,700 (recent); events from ~22 (2002) to ~215 (recent). Observed full-history
totals for 2002–2026 (skip 2021), loaded into staging Cloud SQL:

| table | rows |
|-------|------|
| years | 24 |
| teams | 8,197 |
| team_years | 55,962 |
| events | 2,621 |
| team_events | 106,880 |
| matches | 229,136 |

`Team` rows are inserted once in `post_process` (so the `teams` table is empty
until the build's final step — that is expected, not a hang). On a small Cloud
SQL tier (db-f1-micro) over the cloud-sql-proxy, the per-year "Write DB" step
dominated wall time (~11 s for 2002 → ~5 min for 2016/2017); total run ~99 min
on a cold TBA cache. TBA cached ~2,000+ pickle files under `backend/cache/`.

---

## Step 2 — Current-year reseed + publish (must follow Step 1)

After history exists, recompute the current year so its season-start EPAs use the
real prior two seasons, and publish the corrected blobs:

```python
import src.db.models
from src.data.main import update_curr_year
update_curr_year(partial=False, tba_partial=False)   # GCS enabled, GCS_BUCKET set
```

`partial=False` takes the full-recompute path: `clear_year(CURR_YEAR)` replaces
only the current year (historical rows are untouched), then `write_objs_storage`
publishes the current-year blobs and advances `manifest.json`. **This must run
after Step 1** — if you reseed before history is loaded, the current-year EPAs are
still rookie-seeded. Because `reset_all_years` already processed the current year
in-order (with history), the DB is technically correct after Step 1; Step 2's job
is to (re)publish the corrected current-year blobs to the bucket in a single
controlled cycle.

**What shifts (staging demo).** The visible fix is season-start EPA
(`epa.stats.start`). Before history, every 2026 team seeded at the rookie
constant `23.74` (1 distinct value across all teams); after, seeds are
history-based (2,004 distinct values): 254 → 113.95, 1678 → 131.80, 1323 →
136.19, a mid-pack team → ~33. Current `epa`/`norm_epa` barely move (254:
328.00 → 327.82) because by late season the ratings have already converged to
on-field performance — the seed is what was wrong, and the seed is what the
history fixes. A fresh deployment mid-season (before convergence) would show a
much larger swing in current EPA too.

**GCS billing gotcha (workstation runs).** `storage.Client()` bills GCS requests
to the ADC's *quota project*. If your local `gcloud auth
application-default` quota project differs from the bucket's project and has
billing disabled, writes fail with `403 "The billing account ... is disabled"`
while reads still succeed. Fix by pointing the quota project at the billed
project for the run: `export GOOGLE_CLOUD_QUOTA_PROJECT=<project>` (or `gcloud
auth application-default set-quota-project <project>`). Cloud Run is unaffected
(it bills to its own project). This applies to Step 3 as well.

---

## Step 3 — Historical `hist/` blob backfill (`backfill_blobs.py`)

`backfill_blobs.py` (in `backend/`) exports the per-year historical site blobs
(`team_years/{year}`, `events/{year}`, `event/{key}`, `team/{team}/{year}`) that
the frontend's historical pages read.

```bash
python backfill_blobs.py                 # all past years (2002..CURR_YEAR-1, skip 2021)
python backfill_blobs.py 2015 2016       # specific years
python backfill_blobs.py --force         # ignore the progress checkpoint
```

- **Idempotence / resume.** Progress is a checkpoint object in the bucket
  (`backfill/progress.json`) listing completed years and the epoch. A year already
  marked complete is skipped unless `--force`. Uploads are content-hash gated
  (`upload_historical`), so re-running rewrites nothing that has not changed. An
  interrupted run resumes at the next incomplete year.
- **`HIST_EPOCH`** (`src/constants.py`). A monotonic integer stamped into the
  progress file and the manifest (`hist_epoch`). Bumping it invalidates the
  checkpoint (the next run re-exports every year) and signals the frontend that
  the historical blob set changed — use it when the historical rendering or data
  changes and you need a full re-export. It requires the bucket write creds from
  the prerequisites.
- Requires `GCS_BUCKET` + ADC (and, on a workstation, the
  `GOOGLE_CLOUD_QUOTA_PROJECT` fix above). The DB must already hold the history
  (Step 1).
- **Performance.** `upload_historical` runs one `blob.exists()` + one upload per
  blob, sequentially. A full backfill is ~150k blobs; on a workstation that is
  ~200 blobs/min (hours). The uploads are independent and the bucket client is
  thread-safe (the current-year `_publish` already threads), so wrapping the
  per-year upload loop in a thread pool (e.g. 32 workers) cuts a full backfill to
  minutes. Idempotence is unchanged (each blob path is still checked/written once).
- **Summary blobs are filtered subsets** — do not verify a year by counting the
  `team_years/{year}` or `events/{year}` payload against raw DB rows:
  `_read_team_years` drops `count==0` team_years (registered-but-never-competed;
  e.g. 2015 Recycle Rush had no head-to-head quals, so most teams show count==0
  and the year summary is small), and `_read_events` drops `INVALID`-status
  events. Every team_year still gets its own `team/{team}/{year}` blob. Verify by
  the total blob set (`2 + events + team_years` per year) and a payload
  non-empty/bounded check.

---

## Verification checklist

DB (after Step 1):
- [ ] `years` has one row per season 2002…CURR_YEAR **except 2021**.
- [ ] `team_years` has rows for every one of those years (hundreds early, ~3,700
      recent).
- [ ] `teams` is non-empty (populated in `post_process`).
- [ ] A strong team's `epa_start` for the current year is **not** the rookie
      constant (e.g. 254/1678 seed well above a first-year team).

Current year (after Step 2):
- [ ] `manifest.json` advanced; current-year blobs re-published.
- [ ] Current-year EPAs shifted vs the rookie-seeded values, and the site's
      current-year pages still render.

Historical blobs (after Step 3):
- [ ] `backfill/progress.json` lists all past years; a second run reports 0
      new objects (idempotent).
- [ ] Frontend historical pages render live: `/teams?year=2015`, a 2015 event
      page, and team 254's page showing multi-year history with a sane
      `norm_epa` (254's 2015 season should read as exceptional).

Smoke suite (any environment):
```bash
python3 docs/superpowers/rig/smoke/smoke.py --base-url <api> --data-url <api> \
  --gcs https://storage.googleapis.com --bucket <bucket> --year <CURR_YEAR>
```
