# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working in the `backend/` directory.

> **This is the `cph-staging` branch — what runs in production (the mirror).**
> Its architecture differs from the `cph-master`/upstream line: **no database** —
> DuckDB-over-Parquet serving in a single Cloud Run container, plus offseason
> support and a read-triggered freshness ping. For how and *when* EPA recomputes
> and data refreshes in production, read
> [`docs/superpowers/rig/DATA-REFRESH.md`](../docs/superpowers/rig/DATA-REFRESH.md).

## Commands (run from `backend/`)

```bash
poetry install                          # Install dependencies
uvicorn main:app --reload --port 8000  # Dev API/site server
uvicorn main:app --reload --port 8001  # Data server (emulated separately in dev)
black src/                              # Format
isort src/                              # Sort imports
flake8 src/                             # Lint
pyright                                 # Type check (config at repo root pyrightconfig.json)
```

## Router Structure

Three FastAPI routers are mounted at `/v3` in `main.py`:

| Router | Mount | Purpose |
|--------|-------|---------|
| `src/api/` | `/v3` | **Public REST API** — consumed by external users and the Python package. One file per entity: `team`, `year`, `team_year`, `event`, `team_event`, `match`, `team_match`. |
| `src/data/` | `/v3/data` | **ETL triggers** — internal endpoints that run the data pipeline. `update_curr_year` does a partial (incremental) refresh; `reset_curr_year` does a full refresh; `reset_all_years` rebuilds from 2002. |
| `src/site/` | `/v3/site` | **Frontend-optimized API** — returns pre-shaped data for the website. Also writes results to GCS so the frontend can read blobs from the bucket instead of calling the API at all. |

In dev, the data router calls back to `http://localhost:8001` (a second uvicorn process). **Production (staging) serves all three routers from one Cloud Run container** — see [DATA-REFRESH.md](../docs/superpowers/rig/DATA-REFRESH.md) and [DEPLOY.md](../docs/superpowers/rig/deploy/DEPLOY.md). The leftover App Engine service/dispatch YAML in `deploy/` belongs to the upstream split-service layout and is unused here.

## Data Pipeline (`src/data/`)

For each year, `process_year()` in `src/data/main.py` runs these steps in order:

1. **`process_year_tba()`** (`src/data/tba.py`) — Fetches teams, events, and matches from The Blue Alliance API (with etag-based caching and a local `cache/` directory), returning the raw pipeline objects.
2. **`process_year_avg()`** (`src/data/avg.py`) — Computes average scores for the year object.
3. **`process_year_wins()`** (`src/data/wins.py`) — Computes win/loss/tie records.
4. **`process_year_epa()`** (`src/data/epa/`) — Runs the EPA model:
   - `calc.py` — Calls `src/models/epa/` to compute per-team EPA ratings match by match.
   - `agg.py` — Aggregates match-level EPA up to event and year level.
   - `metrics.py` — Computes normalized EPA and derived statistics.

Results are then published to GCS via `src/google/`: Parquet tables per year (`parquet.py`), the pickled state snapshot the next cycle resumes from (`snapshot.py`), and, for `year == CURR_YEAR`, the zlib-compressed site blobs the frontend reads (`storage.py`, `publish.py`). GCS is the only store.

**Two entry points** in `src/data/main.py`:
- `update_curr_year(partial=True)` — incremental update, resuming from the state snapshot. It refuses to run when the snapshot is unreadable rather than publishing near-empty artifacts over good ones.
- `update_curr_year(partial=False)` / `reset_all_years()` — full recompute from scratch.

## EPA Model (`src/models/epa/`)

The `EPA` class (in `main.py`) extends the `Model` base class (`src/models/template.py`). The base class handles the match processing loop; subclasses implement:
- `start_season()` — initialize per-team ratings from prior years
- `predict_match()` — produce score predictions for both alliances
- `attribute_match()` — compute per-team error attribution after a match
- `update_team()` — update the team's rating

Each team's rating is an `EPARating` (`src/models/epa/math.py`): a per-dimension
mean vector updated by an exponentially-weighted moving average (`add_obs`). It
carries **no variance** — there is no per-team `sd`.

> **Gotcha — this replaced a `SkewNormal` distribution in upstream #412
> (`169330e`, 2026-06-11), and that removed public API fields.** Along with the
> distribution went the `epa_sd`/`epa_skew`/`epa_n` columns on `TeamEvent` and
> `TeamYear`, and with them `epa.total_points.{mean,sd}` (now a bare float) and
> `epa.conf` on `/v3/team_events` and `/v3/team_years`. Clients written against
> the pre-June-2026 API break on this. `Event.epa_sd` is unrelated and still
> exists — it is the stdev *across* a field's teams (`src/data/epa/agg.py`), not
> a team's own uncertainty. Restoring `sd` would mean re-adding model state, not
> just a serializer change.

**EPA dimensions** (for 2016+): `[total, auto, teleop, endgame, rp_1, rp_2, rp_3, tiebreaker, comp_0..comp_9]` — indices 0-17 (max). Pre-2016 only uses `total`.

**Key constants** (`src/models/epa/constants.py`):
- `NORM_MEAN = 1500`, `NORM_SD = 250` — normalized EPA scale
- `ELIM_WEIGHT = 1/3` — elimination matches count less toward rating updates
- `MEAN_REVERSION = 0.4` — cross-season regression to mean

## Game Breakdown (`src/tba/breakdown.py`)

This is the largest file to update each season. It contains:
- `all_keys` — year-keyed dict mapping a year to its list of EPA component names
- `clean_breakdown_{year}()` — parses the raw TBA score breakdown for that year into the standard component format

When adding a new season, add a new entry to `all_keys` and a new `clean_breakdown_{year}` function.

## Entity models and storage (`src/db/`, `src/db_duckdb/`, `src/google/`)

> **There is no relational database.** DB retirement completed 2026-07-27: the
> Cloud SQL instance is deleted and the read/write/transaction code is gone.
> **`src/db/models/` stays load-bearing db-less** — the Parquet writer, the
> state snapshot, and the DuckDB schemas all introspect the attrs/ORM classes
> for column names and types, so their `sqlalchemy` imports are legitimate and
> SQLAlchemy is deliberately retained as a dependency. Only the Postgres driver
> and the CockroachDB dialect were removed.

Six entities are defined in `src/db/models/`: `Team`, `Year`, `TeamYear`,
`Event`, `TeamEvent`, `Match`. (`TeamMatch` was dropped from the pipeline in the
maintainer's June rework.) `src/db/main.py` keeps only the declarative `Base`
those classes hang off.

Where the data actually lives:

| Concern | Where |
|---------|-------|
| Persistence | Parquet, one file per entity per year, in GCS (`src/google/parquet.py`) |
| Query / serving | DuckDB over those Parquet files (`src/db_duckdb/`, schemas introspected from the models) |
| Pipeline resume state | A pickled + zstd-compressed snapshot in GCS (`src/google/snapshot.py`) |
| Frontend reads | Pre-rendered zlib blobs plus a manifest in GCS (`src/google/storage.py`, `publish.py`) |

### Past-year pages: hist blobs are immutable, and a stale one beats the API

Historical (non-current-year) pages are served from `hist/{HIST_EPOCH}/…`
objects written by `write_hist_blobs`, which **skips any object that already
exists**. Recomputing a past year does not replace them: `reprocess-year
YEAR=2025` republishes Parquet and recomputes everything correctly, and the
site API immediately serves the new data — while the website keeps rendering
the old blob.

The failure is silent by construction. `resolveBucketUrl` in
`frontend/src/api/storage.tsx` falls back to `hist/{epoch}/{path}` for any
logical path absent from `manifest.blobs`, and `fetchAndStore` only reaches
the site API when the blob fetch **fails**. A stale-but-valid blob therefore
wins over correct data, with no error anywhere.

This bit the offseason work: after 2025 was reprocessed with per-event
sandbox EPA, `/v3/site/team/254/2025` returned 5 team_events and 81 matches
while `hist/1/team/254/2025` still held 3 and 49, so past-year team pages
showed no offseason events at all.

> **Whenever you change what a past year's blobs should contain — new
> ingestion, a new field, a renderer change — bump `HIST_EPOCH` in
> `src/constants.py` and reprocess the affected years.** Recomputing alone is
> not enough.

Bumping invalidates *every* year, not just the one you changed. That is safe:
years missing from the new epoch fall back to the site API, which serves the
same data from Parquet, and the daily TBA sweep re-exports one year per run
until the epoch refills. Expect a stretch of API-served (slower, not wrong)
historical pages afterward; a full backfill is only worth it if you cannot
wait.

## TBA API (`src/tba/`)

- `main.py` — `get_tba()` wraps TBA HTTP calls with etag support and a local pickle cache (`cache/` dir).
- `read_tba.py` — higher-level functions that call `get_tba()` and return typed objects.
- `breakdown.py` — game-specific score breakdown parsing (see above).
- Auth key is in `src/tba/constants.py` via env var `TBA_KEY`.

### Packed team ids (B/C/D… teams)

Second robots (`frc604B`, `frc498E`) are common at offseason events but have no
team number of their own, and every team key in the schema is an `Integer`.
They are packed by `parse_team()` in `clean_data.py`: **team number in the low
16 bits, suffix letter in the bits above** (`604B` → `(1 << 16) | 604` =
66140). Suffixes run B–Z. `format_team()` is the inverse and is what
`src/data/tba.py` uses to name the synthesized `Team` row; the frontend mirrors
it as `formatTeamNumber` in `frontend/src/utils.tsx`.

Gotchas:
- **Any team id > 65535 is a packed id, not a real team.** Never show one raw —
  run it through `format_team`/`formatTeamNumber`.
- B-teams have no TBA team entry, so they always hit the "synthesize a `Team`
  row" branch in `process_year_tba`.
- Before this existed, a single B-team in an event's matches **dropped the whole
  event** (and `EVENT_BLACKLIST` still holds pre-existing entries like
  `2025miwrc  # B teams in matches`).

### Offseason EPA is per-event sandboxed — and `qual_count` lies about it

Offseason matches no longer skip rating updates. `Model.process_match` wraps
each one in `_sandbox(event)`, which swaps `self.epas`/`self.counts` for
event-scoped copy-on-first-touch forks (`SandboxRatings`/`SandboxCounts` in
`src/models/epa/main.py`). Ratings evolve within the event and touch nothing
else; `post_record_team` gets `ty=None` so `TeamYear` is never stamped.
Containment lives in `src/data/epa/agg.py` — see the
[design spec](../docs/superpowers/specs/2026-07-30-per-event-offseason-epa-design.md).

> **Gotcha — `team_event.qual_count == 0` for EVERY offseason team event**,
> including ones that played a full schedule, because `src/data/wins.py`
> excludes offseason matches from records. Anything gated on "no matches
> played yet" therefore fires for offseason events that *did* play. This bit
> the sandbox once: `calc.py`'s trailing `post_record_team` fallback ran
> outside the sandbox and overwrote every offseason `TeamEvent.epa` with the
> team's frozen season rating, while `epa_mean`/`epa_max` still showed the
> fork evolving. That fallback now runs inside `model._sandbox(...)`. If you
> add another `qual_count`-gated branch, decide explicitly what it should do
> for offseason events.

Verify isolation with
`PYTHONPATH=. PROD=True GCS_BUCKET=statbotics-staging-site poetry run python
scripts/verify_offseason_isolation.py 2025 2026` (every TeamYear rating field
must be bit-identical), and prediction quality with
`python -m src.models.backtest 2025 2026`.

### Offseason quality filters re-run every cycle

The offseason filters live inside `get_events()`, but the data they judge
(rosters, schedules) sits at *other* URLs with their own etags — the
`{year}/events` etag only moves on event metadata. So `process_year_tba` fetches
the event list with `revalidate=True`: on the explicit-etag path a 304 makes
`get_events` return an **empty list**, and an event dropped for "<6 teams"
before its roster went up could never re-enter. The per-event probes are tiered
(`OFFSEASON_REVALIDATION_HOURS`) to keep that cheap.

> **Gotcha — TBA ETags are NOT content fingerprints.** 2026wvrox's roster went
> 0 → 30 teams on event morning with **no change to the `teams/simple` etag**:
> a conditional GET against the stored etag 304'd forever while the pickled
> roster stayed empty, so the `<6 teams` filter dropped the live event on every
> cycle with no path back (2026txdri1 and 2026vagle2 hit the same trap the
> same day). Two consequences, both in code now:
> 1. Quality-filter probes **never send If-None-Match** (`_probe_mode` in
>    `read_tba.py`): they serve a tier-fresh pickle or refetch unconditionally
>    (`get_tba(..., fresh=True)`). Events inside start−1d..end+1d probe fresh
>    every cycle.
> 2. `check_year_partial` ends with a **dropped-candidate sweep**: in-window
>    type-99 events absent from `event_objs` are probed fresh, and the gate
>    escalates when one would now pass the filters — nothing else can trigger
>    a recompute for an event the mirror never ingested (its match etags are
>    unchecked, the events-list etag doesn't move, and no page exists to ping).

## Season Prep

Update these at the start of each new season:
- `src/constants.py`: `CURR_YEAR`, `CURR_WEEK`
- `src/tba/breakdown.py`: add `all_keys[YEAR]` and `clean_breakdown_{year}()`

## Seeding EPA Means from Preseason Events

See `src/data/CLAUDE.md` for the query and instructions.

## GCS / Deployment

- Production is a single Cloud Run container serving all three routers. Deploys go through `make ship` — see [DEPLOY.md](../docs/superpowers/rig/deploy/DEPLOY.md); never hand-roll `gcloud`.
- GCS buckets: `site_v1` (prod), `site_dev_v1` (dev). The frontend reads from these buckets first, falling back to the site API.
- Set env var `PROD=True` to select the production bucket and backend URL.
