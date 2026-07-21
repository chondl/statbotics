# Historical rebuild — db-less full-history build (2002→present)

Audience: a maintainer (re)building the full FRC history for this deployment.
Since DB retirement Phase 3, **GCS is the store and the pipeline is the only
writer**: a single db-less `reset_all_years` produces every artifact the site
serves — per-year Parquet (what DuckDB serves `/v3` from), per-year `hist/`
site blobs (what historical pages render from), and the current-year
snapshot/blobs/Parquet. There is no database to build or reseed, and the old
DB-reading operator scripts (`backfill_parquet.py`, `backfill_blobs.py`) are
retired — their output now comes from `process_year` itself.

---

## What this produces and why it matters

A deployment that has only the current season is wrong in two ways:

1. **Every team is seeded as a rookie.** Season-start EPA (`epa_start`) is
   regressed from the prior seasons. With no history, every team starts at the
   same rookie constant, so current-season EPAs are systematically off for
   strong and weak teams alike. Db-less, `process_year` seeds each year's EPA
   from the prior 4 years' `team_years` Parquet via DuckDB; if those are
   missing, `/info` surfaces `DB_LESS_SEED_INCOMPLETE: true`.
2. **Historical site pages are empty.** `/teams?year=2015`, historical event
   pages, and a team's multi-year history read `hist/{epoch}/…` blobs from the
   bucket that would not exist.

`reset_all_years` (`src/data/main.py`) fixes both in one ordered pass:
2002→CURR_YEAR sequentially (skipping 2021 — no season), so each season's EPA
seeds come from the real prior seasons; each historical year publishes its
Parquet **and** its `hist/` blobs; the current year is processed last and
published after `post_process` (career records, `norm_epa`, `active`,
district all reach GCS).

## Prerequisites

- **TBA API key** (`TBA_AUTH_KEY`). The persistent TBA cache (GCS archives
  under the bucket) makes warm rebuilds cheap and near-zero TBA traffic; a
  genuinely cold cache is thousands of requests (~99 min observed). Pass
  `refresh_tba=true` / `REFRESH_TBA=1` only to force-bypass the cache after
  known upstream corrections.
- **The bucket + write creds.** `GCS_BUCKET` set; ADC with object write. On a
  workstation, mind the quota-project gotcha: if writes 403 with a billing
  error, `export GOOGLE_CLOUD_QUOTA_PROJECT=<bucket project>`.
- **A manifest must already exist.** Historical Parquet publishing
  (`write_parquet`) refuses to invent a fresh `manifest.json` (clobber
  guard). On a **truly fresh bucket**, run one current-year cycle first —
  `curl …/v3/data/reset_curr_year` — which creates the manifest, then run the
  full-history build (it re-publishes the current year, now correctly
  seeded, at the end).
- **No database.** With `DISABLE_DB=True` nothing here touches Postgres. (In
  DB mode the same run additionally rewrites the DB rows — unchanged
  behavior until the Phase 4 decommission.)

## The build

Pause the schedulers so their manifest writes cannot race the rebuild's, then
trigger `reset_all_years` and resume:

```bash
cd docs/superpowers/rig/deploy
make pause-cron
curl -s --max-time 7200 "https://api-statbotics.iterativerefinement.com/v3/data/reset_all_years"
make resume-cron
```

- The endpoint runs synchronously in-request. Cloud Run **services** cap at
  3600 s: fine on a warm TBA cache (minutes); on a cold cache (~99 min) run
  it as a Cloud Run **job** instead — clone the `reprocess-year` job pattern
  in the [Makefile](../rig/deploy/Makefile) with
  `--args "-c" "import src.db.models, src.data.main as dm; dm.reset_all_years()"`
  and a longer `--task-timeout`. A Cloudflare edge 524 does not stop the
  server-side run; poll `make logs | grep 'Write Storage'`.
- Per year it prints `{year} Write Parquet` and `{year} Write Hist Blobs`;
  the run ends with the current-year `Write Snapshot` / `Write Storage`.
- **Manifest cadence:** each historical year does one manifest
  read-modify-write. That is safe at the pipeline's minutes-per-year cadence,
  but do not wrap rapid manual manifest writers in a tight loop — the
  manifest carries `Cache-Control: max-age=60`.

### `hist/` blob semantics (write-once per epoch)

Hist blobs are immutable within a `HIST_EPOCH` (`src/constants.py`):
`upload_historical` skips objects that already exist, so a rebuild over a
populated bucket only fills gaps — it does **not** overwrite existing hist
payloads. When historical rendering or data changes and the exported set must
actually be replaced, **bump `HIST_EPOCH`**: the pipeline then writes the
whole set fresh under `hist/{new_epoch}/…`, and the manifest's `hist_epoch`
stamp (maintained by every current-year publish) repoints readers. Uploads
are thread-pooled; a full-history export is ~150k objects.

### Single-year rebuild

For one changed historical year, prefer the targeted job — same pipeline,
same artifacts, plus the mandatory chained full current-year re-render (team
blobs embed full history):

```bash
make reprocess-year YEAR=2025            # add DISABLE_DB=True to run db-less pre-flip
```

## Verification checklist

- [ ] Logs show `{year} Write Parquet` + `{year} Write Hist Blobs` for every
      year 2002…CURR_YEAR−1 except 2021, then the current-year publish.
- [ ] `manifest.json` has `parquet/{year}/*.parquet` refs for all years
      (6 tables/year) and a current `hist_epoch`.
- [ ] `/info` shows no `DB_LESS_SEED_INCOMPLETE` / `DB_LESS_PUBLISH_SKIPPED`.
- [ ] A strong team's current-year `epa_start` is not the rookie constant
      (254/1678 seed well above a first-year team).
- [ ] Frontend historical pages render from the bucket: `/teams?year=2015`, a
      2015 event page, team 254's multi-year history with sane `norm_epa`.
- [ ] Summary blobs are **filtered subsets** — don't count
      `team_years/{year}`/`events/{year}` payloads against raw rows
      (`_read_team_years` drops `count==0`, `_read_events` drops INVALID).
- [ ] Smoke suite:

```bash
python3 docs/superpowers/rig/smoke/smoke.py --base-url <api> --data-url <api> \
  --gcs https://storage.googleapis.com --bucket <bucket> --year <CURR_YEAR>
```
