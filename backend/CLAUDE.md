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

Results are then published to GCS via `src/google/`: Parquet tables per year (`parquet.py`), the pickled state snapshot the next cycle resumes from (`snapshot.py`), and the zlib-compressed site blobs the frontend reads (`storage.py`, `publish.py`). GCS is the only store. How the site blobs go out depends on the kind of year (`process_year`'s branches):
- **`CURR_YEAR`** — versioned `v2/` blobs plus Parquet through the manifest, then the snapshot.
- **The trailing year (`CURR_YEAR − 1`) while the trailing window is open** (until Dec 28 00:00 America/Los_Angeles of the trailing year; rule in `src/data/trailing.py`) — a *live year* too: current-year event statuses (Upcoming/Ongoing), and the **trailing publish** (`src/google/trailing_publish.py`): site blobs and Parquet as versioned `v2/` blobs in one manifest write, then the snapshot. Never hist blobs.
- **Any other (past) year** — Parquet, then `year_epoch.publish_past_year`, which writes the site blobs under the year's **year epoch** (`hist/{e}/…`, see the gotcha below).

Use `trailing.is_live_year(year)`, not `year == CURR_YEAR`, for behaviour an ongoing event needs; the comment block at the top of `src/data/trailing.py` classifies every `CURR_YEAR` use.

**Entry points** in `src/data/main.py`:
- `update_curr_year(partial=True)` — incremental update, resuming from the state snapshot. It refuses to run when the snapshot is unreadable rather than publishing near-empty artifacts over good ones.
- `update_curr_year(partial=False)` / `reset_all_years()` — full recompute from scratch.
- `update_trailing_year(full)` — one trailing cycle; `close_trailing_year()` — window close, which retires the trailing year into a past year after the window closes. The gate (`GET /v3/site/update_curr_year`, `src/data/router.py` `trailing_gate_action`) schedules both; see [DATA-REFRESH.md](../docs/superpowers/rig/DATA-REFRESH.md) §3.
- `reprocess_year(year)` — one past year (the sweep and `make reprocess-year`); for a live trailing year it runs a full trailing cycle instead.

**Pipeline lock** (`src/data/pipeline_lock.py`). The heavy pipelines take `PIPELINE_LOCK` through `try_hold()`, never blocking: the trailing endpoints answer `{"status": "busy"}`, and the current-year cycle, the sweep and `refresh_teams` skip and print `PIPELINE BUSY: skipped <name>`. `try_hold` is re-entrant per thread, so a sweep's reprocess can chain the current-year cycle and `refresh_teams` in the same thread. **Invariant: a held region never `await`s** — every `async def` handler runs on the one event-loop thread, so a coroutine awaiting under the lock would let another coroutine pass `try_hold` as the "owner". Hold it only around synchronous pipeline code; the trailing endpoints are plain `def` (FastAPI threadpool) for that reason.

**Chained current-year re-render.** When a trailing cycle or window close changes the trailing year's `parquet/{Y}/team_years` digest, team pages (which embed every season) need a full current-year re-render plus `refresh_teams`. From the endpoints it is a separate HTTP request, `GET /v3/data/reset_curr_year`, sent from a daemon thread after the lock is released (inside the lock it would skip as busy). From `reprocess_year` / a Cloud Run job it runs inline (`chain="inline"`), because the job's process exits when the driver returns.

**Manifest writes.** Every `manifest.json` write goes through `storage.update_manifest(mutate)`: a read pinned to one generation, the idempotent `mutate`, a write with `if_generation_match` (the manifest precondition); on a 412 it re-reads and re-applies, up to 5 retries, then raises and counts `MANIFEST_CONFLICTS` in `/info`. `read_manifest` returns `None` only when the object does not exist; any other failure raises `ManifestReadError` and the writer aborts (planning from an empty manifest would erase `hist_epochs`). Digest skips — `plan_uploads` and `_fold_parquet` deciding a path is unchanged — compare against the manifest read at plan time, not the one `update_manifest` re-reads at commit. That is safe because the planners run under `PIPELINE_LOCK`, so no other pipeline in the process changes those entries between plan and commit; the commit applies only the plan's delta, so another process's entries survive.

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

### Past-year pages: hist blobs are immutable, so a change moves the year epoch

Past-year pages are served from `hist/{e}/…` objects, which are written with
`Cache-Control: public, max-age=31536000, immutable` and must never be
overwritten once readers resolve them. `e` is the year's **year epoch**,
`max(hist_epochs[year] ?? 0, hist_epoch)`: `hist_epochs` is a per-year map in
the manifest, `hist_epoch` the global epoch stamped from `HIST_EPOCH`
(`src/constants.py`). The rule has one backend copy,
`src/google/year_path.py` (`year_from_path`, `effective_hist_epoch`), mirrored
by `frontend/src/api/yearFromPath.ts`; both test suites check the shared
vectors in `tests/data/year_from_path_vectors.json`.

Every past-year reprocess — the daily sweep, `make reprocess-year`, window
close, `reset_all_years` — publishes through
`year_epoch.publish_past_year(year, rendered)`, which compares each rendered
page with the object under `e` (MD5) and prints
`YEAR EPOCH {year} {outcome} epoch={e}`:
- `unchanged` — nothing written;
- `gap_filled` — no object under `e` (a cold year): all written under `e`;
- `advanced` — some page missing or different: all written under `e + 1`
  (overwriting leftovers of a crashed advance; nobody resolves `e + 1` yet),
  then `hist_epochs[year] = e + 1` in one manifest write.

In every outcome the same manifest write removes the year's non-Parquet
`manifest.blobs` entries, which would otherwise outrank its hist blobs. It
refuses a live year (`ValueError("live year")`): no hist blob is ever written
for one. So recomputing a past year now reaches its pages; no `HIST_EPOCH`
bump is needed for a data change.

The history behind this: `write_hist_blobs` used to skip existing objects, so
after 2025 was reprocessed with per-event sandbox EPA, `/v3/site/team/254/2025`
returned 5 team_events and 81 matches while `hist/1/team/254/2025` still held
3 and 49. The failure is silent by construction: `resolveBucketUrl` in
`frontend/src/api/storage.tsx` resolves `manifest.blobs` first, then
`hist/`, and `fetchAndStore` reaches the site API only when the blob fetch
**fails**, so a stale-but-valid blob wins over correct data with no error.
After the 2027 rollover `2026mmr` hit two more traps: the last current-year
cycle of 2026 left frozen `event/*`, `events/2026` and `team_years/2026`
manifest entries that outranked every hist blob, and `get_event_status`
treated a past-year event with no matches as Invalid. Trailing cycles, window
close and year epochs fix all three; the design is
[the trailing-year refresh spec](../docs/superpowers/specs/2026-11-24-trailing-year-refresh-design.md).

> **`HIST_EPOCH` is now only the lever for a renderer change across all
> years.** A global bump must exceed `max(hist_epochs)`: the year epoch is a
> `max`, so a year already advanced to an epoch at or above the new
> `HIST_EPOCH` keeps serving its older render. `/info` reports
> `HIST_EPOCH_SHADOWED: true` when the deployed `HIST_EPOCH` does not exceed
> every entry. The next manifest write drops `hist_epochs` entries at or
> below the global epoch.

A global bump invalidates every year. Years missing from the new epoch fall
back to the site API, which serves the same data from Parquet. The daily TBA
sweep does **not** refill the new epoch: `revalidate_tba` reprocesses a year
only when a TBA path returned 200 (`sweep.py`), so an unchanged year stays
API-served until something reprocesses it. Site-blob renderers keep their
input order (no sorting), so a re-render of unchanged data is byte-identical
and does not advance a year epoch.

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
> 2. `check_year_partial` ends with a **dropped-candidate sweep**: type-99
>    events inside their event date window and absent from `event_objs` are probed fresh, and the gate
>    escalates when one would now pass the filters — nothing else can trigger
>    a recompute for an event the mirror never ingested (its match etags are
>    unchecked, the events-list etag doesn't move, and no page exists to ping).

## Season Prep

Season prep happens in two steps: a **pre-kickoff rollover**, which can
ship as soon as TBA posts the new season's events, and the **game breakdown**,
which needs the announced game. (There is no `CURR_WEEK` constant any more.)

**Pre-kickoff rollover** (done for 2027 on 2026-11-06):
- `src/constants.py`: `CURR_YEAR`.
- `src/breakdown.py`: a placeholder `key_to_name[YEAR]` with only the
  standard keys (auto/teleop/endgame, `rp_1..rp_3`, tiebreaker). **Required:**
  an unlisted year gets the pre-2016 single-key layout, and every
  `year >= 2016` path crashes looking up `rp_1`.
- `src/data/avg.py`: placeholder means, gated on "no week-1 matches".
  **Required:** with no matches `score_sd` is 0 and `get_init_epa` divides
  by it. Point EPAs are on an arbitrary scale until real data exists; the
  team order (prior seasons' `norm_epa`) is what is meaningful.
- Deploy, then `make reprocess-curr-year`: the old year's state snapshot
  does not carry over, so hourly partial cycles skip (`PARTIAL_SKIPPED` in
  `/info`) until a full reset writes the new year's snapshot.
- Nothing to do for the old year. It becomes the **trailing year** and stays
  a live year until **Dec 28 00:00 America/Los_Angeles** of that year (the
  trailing window, a code rule in `src/data/trailing.py`): the gate's first
  tick after the deploy runs the bootstrap (a full trailing cycle; `make
  update-curr-year` answers `"trailing": "full"`), and from then on its
  offseason events refresh like current-year events. On the first gate tick
  after Dec 28 00:00 PT, window close retires it: a final fresh cycle,
  its site blobs under a new year epoch, its non-Parquet manifest entries
  removed, `trailing_year: null`. A rollover shipped after Dec 28 skips the
  trailing window: the first gate tick runs window close straight away,
  because the old year's current-year manifest entries are still there. Do
  **not** run `make reprocess-year
  YEAR=<old year>` as part of the rollover. Check with `make info`
  (`TRAILING_YEAR`, `TRAILING_WINDOW_OPEN`, `TRAILING_STATUS`) and
  `make verify-trailing YEAR=<old year> EVENT=<event key>`.
- Districts: compare TBA's `/districts/{YEAR}` with `VALID_DISTRICTS`; an
  unknown one is flattened to no district and logged as
  `WARNING: unknown district`.

**After the game is announced:**
- `src/breakdown.py`: real `key_to_name[YEAR]` and `derived_breakdown`.
- `src/tba/breakdown.py`: `clean_breakdown_{year}()` and its dispatch line.
- `src/data/avg.py`: replace the placeholder means (game-manual estimates,
  then week 0 — see `src/data/CLAUDE.md`).

Note for 2027 prep: the war-delayed 2026 Israeli events that ran after Worlds
(`2026isde1`, `2026isde2`, `2026iscmp`) are forced to OFFSEASON via
`EVENT_TYPE_OVERRIDES` (`src/tba/constants.py`) — a deliberate owner decision.
Offseason typing already keeps them out of season standings and out of the
2027 start-EPA seed; do not "fix" them back to district types.

## Seeding EPA Means from Preseason Events

See `src/data/CLAUDE.md` for the query and instructions.

## GCS / Deployment

- Production is a single Cloud Run container serving all three routers. Deploys go through `make ship` — see [DEPLOY.md](../docs/superpowers/rig/deploy/DEPLOY.md); never hand-roll `gcloud`.
- GCS buckets: `site_v1` (prod), `site_dev_v1` (dev). The frontend reads from these buckets first, falling back to the site API.
- Set env var `PROD=True` to select the production bucket and backend URL.
