# Statbotics Backend Re-Architecture: DuckDB + GCS Static Files

**Date:** 2026-04-11
**Status:** ⚠️ PARTIALLY SUPERSEDED

> The DuckDB-over-Parquet serving layer described here **shipped** and runs in
> production. But this spec's central EPA proposal — **incremental, O(new-matches)
> updates with ~30s latency** (Goals 2, 8; Non-Goal "30-second polling") — was
> **reversed**. The shipped system keeps a deterministic **full-season EPA replay
> every cycle**; see the [2026-07-09 spec §6](2026-07-09-epa-consistency-and-bucket-first-serving-spec.md)
> for that decision and [DATA-REFRESH.md](../rig/DATA-REFRESH.md) for how EPA
> actually recomputes and what triggers it. Read this file for the storage/serving
> architecture, not for the update model.

## Problem

The current backend uses CockroachDB as both a compute scratchpad (the EPA pipeline reads/writes the entire season on every update) and a serving layer (the API queries it for every reader). These two uses conflict under load:

- Every partial update recomputes EPA for all matches in the season (O(season)), not just new matches
- Upserts rewrite every column on every row, even unchanged ones
- Connection pool (default size 5) is shared between the write pipeline and API readers
- Three caching layers (alru_cache, GCS blobs, frontend IndexedDB) have bugs causing stale data and split-brain inconsistency
- No concurrency protection on updates; concurrent refreshes can corrupt data

The data volume is small (~4K teams, ~200 events, ~25K matches per year) and the write rate is low (~2K matches/day during peak season). The system should be trivially fast but isn't.

## Goals

1. API reads for the current year are sub-millisecond (in-memory)
2. Updates process only new matches, not the full season (O(new matches))
3. Single source of truth -- no split-brain between caching layers
4. Public REST API contract (`/v3/*`) is unchanged
5. Frontend-optimized API (`/v3/site/*`) is unchanged
6. Infrastructure simplifies from CockroachDB + 3 App Engine services + GCS blobs to 1 Cloud Run service + 1 GCS bucket (includes platform migration from App Engine to Cloud Run)
7. 100 requests/second sustained from a single process
8. Updates appear within ~30 seconds of a match being posted to TBA
9. Incremental migration with shadow-traffic validation before cutover

## Non-Goals

- Horizontal write scaling (single writer is sufficient)
- Sub-second update latency (30-second polling interval is acceptable)
- Changing the frontend framework or component structure

## Architecture Overview

```
                    ┌──────────────┐
                    │   TBA API    │
                    └──────┬───────┘
                           │ (polled every 30s during events)
                           v
┌──────────────────────────────────────────────┐
│           Cloud Run Service (single)         │
│                                              │
│  ┌─────────────┐    ┌─────────────────────┐  │
│  │  Refresh     │    │   DuckDB            │  │
│  │  Loop        │───>│   (in-memory)       │  │
│  │  (background │    │                     │  │
│  │   task,      │    │  current year tables │  │
│  │   mutex)     │    │  epa_state table     │  │
│  └─────────────┘    │  watermark table      │  │
│                      └──────┬──────────────┘  │
│                             │                  │
│  ┌──────────┐  ┌───────────┴──┐               │
│  │ /v3 API  │  │ /v3/site API │               │
│  │ (public) │  │ (frontend)   │               │
│  └──────────┘  └──────────────┘               │
└──────────────────────┬───────────────────────┘
                       │ snapshot upload / historical reads
                       v
              ┌────────────────┐
              │  GCS Bucket    │
              │                │
              │  historical/   │  Parquet files per year
              │  current/      │  DuckDB snapshot + manifest
              │  teams.parquet │  Master team table
              └────────────────┘
```

## Storage Layout

```
statbotics-data/
├── historical/
│   ├── 2002/
│   │   ├── matches.parquet
│   │   ├── team_matches.parquet
│   │   ├── team_years.parquet
│   │   ├── team_events.parquet
│   │   ├── events.parquet
│   │   └── year.json
│   ├── 2003/
│   │   └── ...
│   └── 2025/
│       └── ...
├── current/
│   ├── snapshot.duckdb
│   └── manifest.json          # version counter, last update timestamp
└── teams.parquet               # master team table (~12K rows)
```

### Historical files (Parquet)

One set of Parquet files per completed season. These are immutable -- once a season ends, the files never change. DuckDB queries them directly via its `httpfs`/GCS extension, pushing filters down into the Parquet reader.

Approximate sizes per year: matches ~6 MB, team_matches ~10 MB, team_years ~1 MB, team_events ~1 MB, events <1 MB. Total historical archive across 24 years: ~450 MB.

### Current year (DuckDB snapshot)

The current year's data lives in-memory in the DuckDB process. The snapshot file on GCS is for cold-start recovery -- on boot, the process downloads and loads it. During operation, the snapshot is uploaded to GCS after each successful refresh (fire-and-forget, does not block readers).

### Master team table

Cross-year team metadata (team number, name, country, state, rookie year, active status). ~12K rows, <1 MB. Loaded into DuckDB at startup.

## DuckDB Schema

### Data tables

The same seven entity tables as today, with the same columns. The only change is the storage engine (DuckDB in-memory instead of CockroachDB):

- `years` -- 1 row per season
- `teams` -- ~12K rows total (master table, loaded from Parquet)
- `team_years` -- ~4K rows for current year
- `events` -- ~200 rows for current year
- `team_events` -- ~12K rows for current year
- `matches` -- ~25K rows for current year (200 events x 100-150 matches each)
- `team_matches` -- ~150K rows for current year (6 teams per match)

### ETag table

```sql
CREATE TABLE etags (
    path VARCHAR PRIMARY KEY,
    etag VARCHAR
);
```

Stores TBA API ETags per endpoint path. Used to send `If-None-Match` headers on TBA requests, enabling 304 Not Modified responses that avoid re-fetching unchanged data. Without this, every poll cycle would fully re-fetch all active event data, risking TBA rate limits.

On cold start, ETags are lost (recoverable -- the first poll cycle does a full fetch and re-populates them). This is acceptable since cold starts are rare.

### EPA model state table

```sql
CREATE TABLE epa_state (
    team INTEGER PRIMARY KEY,
    mean_0 FLOAT, mean_1 FLOAT, ... mean_25 FLOAT,
    var_0 FLOAT, var_1 FLOAT, ... var_25 FLOAT,
    skew FLOAT,
    skew_i INTEGER,   -- skew index (currently always 0, stored for completeness)
    n FLOAT,
    count INTEGER      -- qual match count, used for learning rate decay
);
```

~4K rows. Stores the SkewNormal distribution parameters so the EPA model can resume from a checkpoint instead of replaying all matches. See "Incremental EPA: Checkpoint/Resume Protocol" below for the full reconstruction procedure.

### Watermark table

```sql
CREATE TABLE watermark (
    id INTEGER PRIMARY KEY DEFAULT 1,
    last_match_time INTEGER,
    last_match_key VARCHAR,
    last_poll_time INTEGER
);
```

Single row. Tracks what has been processed and when TBA was last polled. The watermark uses both `last_match_time` and `last_match_key` because multiple matches can share the same timestamp -- the key provides a tiebreaker for ordering.

### Note on year 2021

There was no FRC season in 2021 (COVID). No historical Parquet files exist for 2021. The historical export script skips it, and cross-year glob queries (`historical/*/`) tolerate missing years -- DuckDB's `read_parquet` with glob patterns gracefully handles missing directories.

## Data Flow

### Cold start

1. Download `current/snapshot.duckdb` from GCS (~10-15 MB)
2. Load into DuckDB in-memory mode
3. Load `teams.parquet` into the `teams` table
4. Read watermark to know the last processed state
5. Process is ready to serve requests

Estimated cold-start time: 2-3 seconds (dominated by GCS download).

### Request-triggered refresh (stale-while-revalidate)

On each incoming API request:

1. Check: `now > watermark.last_poll_time + POLL_INTERVAL`?
   - `POLL_INTERVAL` = 30 seconds if any event has status "Ongoing", 300 seconds otherwise
2. If no: serve from current DuckDB state. Done.
3. If yes: serve from current DuckDB state, and attempt to acquire the refresh lock (non-blocking).
   - Lock busy: serve stale. Another refresh is already running.
   - Lock acquired: spawn a background asyncio task to refresh.

### Background refresh (incremental)

1. **Poll TBA** with ETags for active events. If all return 304 Not Modified, update `last_poll_time`, release lock. Done.
2. **Fetch new match data** from TBA for events that returned new data.
3. **Detect new vs. changed matches:**
   - New matches: `match.key` not in DuckDB
   - Score corrections: existing match where score fields differ from TBA response
4. **If only new matches (common case):**
   - Load EPA state from `epa_state` table, reconstruct SkewNormal dicts
   - Process only matches with `time > watermark.last_match_time` through the EPA model
   - Write new/updated rows to DuckDB tables (matches, team_matches, team_events, team_years, events, year)
   - Write updated EPA state back to `epa_state`
   - Update watermark
   - Commit transaction (readers atomically see new state)
5. **If score corrections detected (rare):**
   - Fall back to full-season recompute from TBA data
   - Rebuild all tables and EPA state from scratch
   - This is the same cost as today's update, but happens a few times per season at most
6. **Async:** upload DuckDB snapshot to GCS for cold-start recovery. Fire-and-forget.

### Season rollover

Triggered manually or at end of season:

1. Export current year's DuckDB tables to Parquet files
2. Upload to `historical/{year}/` on GCS
3. Reset DuckDB for the new season (empty tables + fresh EPA initialization)

## Incremental EPA: Checkpoint/Resume Protocol

This is the core mechanism that reduces update cost from O(all matches) to O(new matches).

### What state is saved

The `epa_state` table stores everything needed to reconstruct an `EPA` model instance mid-season:

- Per-team `SkewNormal` parameters: `mean_0..25`, `var_0..25`, `skew`, `skew_i`, `n`
- Per-team qual match count: `count` (drives learning rate decay via `EPA.percent_func`)

The year-level constant `EPA.k` is derived from the year number (`EPA.k_func(year)`) and does not need to be stored.

### How the model is reconstructed (without re-running `start_season()`)

When resuming from a checkpoint, the model is reconstructed directly from stored state rather than calling `start_season()`, which would re-initialize from prior years:

```python
model = EPA()
model.year_obj = year_obj  # loaded from DuckDB years table
model.year_num = year_obj.year
model.num_teams = 2 if year_obj.year <= 2004 else 3
model.k = EPA.k_func(year_obj.year)

# Reconstruct from epa_state table (NOT from start_season)
model.epas = {}
model.counts = {}
for row in conn.execute("SELECT * FROM epa_state").fetchall():
    mean = np.array([row.mean_0, ..., row.mean_25], dtype=np.float32)
    var = np.array([row.var_0, ..., row.var_25], dtype=np.float32)
    model.epas[row.team] = SkewNormal.from_params(mean, var, row.skew, row.skew_i, row.n)
    model.counts[row.team] = row.count
```

`start_season()` is only called once -- when a new season begins and there is no checkpoint.

### How the match processing loop resumes

New matches are identified and ordered using both time and key:

```python
watermark = conn.execute("SELECT * FROM watermark").fetchone()

new_matches = [m for m in tba_matches
               if (m.time, m.key) > (watermark.last_match_time, watermark.last_match_key)]
new_matches.sort(key=lambda m: (m.time, m.key))
```

Using the tuple `(time, key)` ensures deterministic ordering even when matches share timestamps.

For each new match, the existing `model.process_match()` is called, which:
1. Calls `pre_record_team()` -- writes current EPA to the TeamMatch row
2. Calls `predict_match()` and `record_match()` -- writes predictions to the Match row
3. Calls `attribute_match()` and `update_team()` -- updates the SkewNormal state
4. Calls `post_record_team()` -- writes updated EPA to TeamMatch, TeamEvent, TeamYear rows

### How aggregations are updated incrementally

The match-level EPA updates naturally flow to TeamEvent and TeamYear via `post_record_team()`. However, the aggregation step (`src/data/epa/agg.py`) computes event-level statistics (epa_max, epa_top_8, epa_mean, etc.) and year-level rankings (epa_rank, percentile) across all teams.

For incremental updates:
- **Event-level aggregations** (epa_max, epa_mean, etc.): Recomputed for affected events only (events that had new matches). This scans the team_events table for that event -- ~60 rows per event.
- **Year-level rankings** (epa_rank, percentile, country/state/district ranks): Recomputed across all team_years for the current year. This is a sort of ~4K rows in DuckDB, which takes microseconds.

### Score correction detection

A score correction is detected when a match exists in DuckDB but the following fields differ from the TBA response: `red_score`, `blue_score`, or any score breakdown field (`red_no_foul`, `red_auto`, `red_teleop`, `red_endgame`, and their blue equivalents).

Fields that can change without triggering a full recompute: `predicted_time`, `video`, alliance member changes before a match is played (status = "Upcoming").

## Complex Queries

### Noteworthy matches and upcoming matches

The current system has complex cross-table queries in `src/db/functions/` for noteworthy matches (high-EPA matchups, upsets) and upcoming matches (sorted by various EPA metrics). These use joins, aggregations, and computed columns.

In the new architecture, these queries run directly against in-memory DuckDB for the current year. DuckDB handles joins and aggregations natively. Since the source tables are entirely in memory, these complex queries are still sub-millisecond. No pre-computation or caching is needed.

### Site router pre-joined responses

The `site/` router returns pre-joined responses (e.g., event + matches + team_events + team_matches in one call). These currently make multiple cached DB queries. In the new system, they either make multiple DuckDB queries (each sub-millisecond) or a single joined query. The response shaping logic (`_read_event()`, `_read_team_years()`, etc.) is unchanged.

## Historical Team Metadata

Historical Parquet files contain denormalized team metadata (name, country, state) as it was at the time the season was archived. If a team's metadata changes later (e.g., name update), the historical files are not updated -- they reflect the state at archival time.

For API consumers querying historical data, this is acceptable: the data was correct when recorded. If exact current metadata is needed alongside historical performance data, the consumer can join with the `/v3/team/{team}` endpoint, which always reflects current metadata from the master team table.

## Query Layer

### Current year queries (hot path)

Direct SQL against in-memory DuckDB tables. The existing API filter/sort/paginate parameters map to parameterized SQL:

```python
def get_team_years(year, country=None, metric=None, limit=None, offset=None):
    query = "SELECT * FROM team_years WHERE year = ?"
    params = [year]
    if country:
        query += " AND country = ?"
        params.append(country)
    if metric:
        assert metric in ALLOWED_TEAM_YEAR_METRICS  # column whitelist
        query += f" ORDER BY {metric} DESC"
    if limit:
        query += " LIMIT ?"
        params.append(limit)
    if offset:
        query += " OFFSET ?"
        params.append(offset)
    return conn.execute(query, params).fetchall()
```

Expected latency: <1 ms.

### Historical year queries (cold path)

DuckDB queries Parquet files on GCS directly:

```python
query = """
    SELECT * FROM read_parquet(
        'gs://statbotics-data/historical/2015/team_years.parquet'
    ) WHERE team = ?
"""
```

First access to a historical year has network latency (downloading 1-5 MB Parquet file). DuckDB caches the file after first read. Subsequent queries for the same year are fast.

### Cross-year queries

DuckDB handles mixed in-memory + remote Parquet in a single query:

```python
query = """
    SELECT * FROM team_years WHERE team = ?
    UNION ALL
    SELECT * FROM read_parquet(
        'gs://statbotics-data/historical/*/team_years.parquet'
    ) WHERE team = ?
"""
```

The glob pattern scans all years' Parquet files with predicate pushdown.

### Column validation

The `metric` parameter (used for sorting) is validated against a per-entity whitelist of column names before being interpolated into SQL. All other parameters use parameterized queries.

### No server-side caching needed

With in-memory DuckDB, reads are microseconds. The `alru_cache` layer and its bugs are removed entirely. The only caching is optional client-side IndexedDB in the frontend (short TTL for current year, longer for historical).

## Concurrency Model

- **Single process, single writer.** One Cloud Run instance handles all traffic.
- **asyncio.Lock** prevents concurrent refreshes. Non-blocking acquire: if the lock is held, the request serves stale data and doesn't wait.
- **DuckDB transactions** provide snapshot isolation: readers see the old state until the refresh transaction commits.
- **100 rps capacity:** In-memory DuckDB reads are microseconds of CPU. A single FastAPI process on 2 vCPUs can handle thousands of rps. 100 rps uses <2% CPU on reads.
- **Background refresh** runs in an asyncio task. DuckDB's single-writer/multiple-reader model means reads are not blocked during writes.
- **Scaling path (if ever needed):** The snapshot-to-GCS upload already exists for cold-start recovery. Read replicas can poll GCS for the latest snapshot on a 30-second interval, serving from their own in-memory DuckDB copy. The writer is the only process that polls TBA and computes EPA. This is a configuration change, not an architecture change.

### DuckDB connection management in async context

DuckDB queries are CPU-bound and would block the asyncio event loop if run directly. The connection strategy:

- **One primary DuckDB connection** shared across the process.
- **API read handlers** run DuckDB queries via `asyncio.to_thread()`, which offloads them to a thread pool. Each thread uses `conn.cursor()` to get a thread-local cursor from the shared connection. DuckDB supports concurrent read cursors from a single connection.
- **The background refresh task** also runs via `asyncio.to_thread()`. It uses a write transaction on the same connection. DuckDB's MVCC ensures concurrent read cursors see the pre-transaction snapshot until the write commits.

This avoids blocking the event loop while keeping connection management simple (one connection, multiple cursors).

## Router Structure

| Router | Mount | Purpose | Change |
|--------|-------|---------|--------|
| `api/` | `/v3` | Public REST API | Backed by DuckDB instead of CockroachDB |
| `site/` | `/v3/site` | Frontend-optimized pre-joined responses | Same, backed by DuckDB |
| ~~`data/`~~ | ~~`/v3/data`~~ | ~~ETL triggers~~ | Removed. Refresh is internal. |

Deployment collapses from three App Engine services to one Cloud Run service. This is also a platform migration -- App Engine's `dispatch.yaml` routing and scaling model are replaced by Cloud Run's simpler container-based model.

## Testing Strategy

The backend currently has zero test coverage -- no test files, no pytest config, no test dependencies. Before making any architectural changes, we need comprehensive tests to verify that behavior is preserved. Testing is organized in three phases that map directly to the first three rounds of pull requests in the migration plan.

### Current testability assessment

| Layer | DB-free? | Testable today? |
|-------|----------|----------------|
| `src/models/epa/math.py` (SkewNormal, sigmoids) | Yes | Yes -- pure numpy/scipy |
| `src/tba/` (HTTP client, breakdown parsing) | Yes | Yes -- mock HTTP |
| `src/models/epa/` (EPA model logic) | No -- imports db.models | No |
| `src/data/` (pipeline orchestration) | No -- reads/writes DB directly | No |
| `src/api/`, `src/site/` (serving) | No -- queries DB | No |

The core problem: the plain dataclasses (`Match`, `TeamYear`, etc.) are generated from SQLAlchemy ORM classes via `generate_attr_class()` in `src/db/models/main.py`. The EPA model imports these types, so it can't be instantiated without the ORM layer loaded.

### Testing Phase 1: Unit tests and API contract tests (no refactoring needed)

**Goal:** Establish test infrastructure, cover the code that is already testable in isolation, and lock down the API response contract. All of this is safe, non-controversial work suitable for a first round of PRs. The API contract tests are written early -- before any refactoring -- so they protect every subsequent change.

**Test infrastructure setup:**
- Add pytest, pytest-asyncio, pytest-cov, responses (HTTP mocking) to pyproject.toml dev dependencies
- Create `backend/tests/` directory with `conftest.py`
- Add pytest configuration to `pyproject.toml`
- Add a `make test` / `poetry run pytest` command

**PR 1: Unit tests for decoupled code**

1. **`tests/models/epa/test_math.py`** -- SkewNormal class
   - Construction with mean/var/skew/n parameters
   - `add_obs()` correctly updates distribution after an observation
   - `from_params()` round-trips correctly (construct -> extract params -> reconstruct -> compare)
   - `get_skew_normal_95_conf_interval()` returns reasonable bounds
   - `unit_sigmoid`, `zero_sigmoid`, `inv_unit_sigmoid` -- known input/output pairs
   - Edge cases: zero variance, extreme skew, n=0

2. **`tests/tba/test_breakdown.py`** -- Score breakdown parsing
   - Snapshot tests for each year's `clean_breakdown_{year}()` function
   - Feed in a known TBA match breakdown dict, verify the output array
   - Cover years 2016-2026 (pre-2016 uses a simpler path)
   - Verify `all_keys` mapping is consistent with breakdown functions

3. **`tests/tba/test_main.py`** -- TBA HTTP client
   - Mock `requests.Session` to test `get_tba()`
   - Test ETag flow: first request returns data + etag, second request with etag returns 304
   - Test pickle cache: verify cache hit returns stored data without HTTP call
   - Test cache miss: verify HTTP call is made and result is cached
   - Test error handling: HTTP errors, malformed responses

4. **`tests/models/epa/test_constants.py`** -- Verify constants haven't drifted
   - `NORM_MEAN`, `NORM_SD`, `ELIM_WEIGHT`, etc. match expected values
   - `k_func()` and `margin_func()` return expected values for known years

**PR scope:** ~500-800 lines of test code plus pyproject.toml changes. Pure additions, no changes to existing code.

**PR 2: API contract tests**

Tests that validate every endpoint's response schema and values. These serve two purposes: (a) they document the exact API contract, and (b) they become the safety net for all subsequent refactoring and backend changes.

The contract tests are written against an abstract client interface so they can run in two modes:

**Local mode (`make test`):** Uses FastAPI's `TestClient` to call routes directly in-process, no HTTP server needed. This is the default and runs as part of the normal test suite. Requires a database connection (or, after the DuckDB migration, just the in-memory DuckDB) but no external services.

```python
# tests/contract/conftest.py
import pytest
from fastapi.testclient import TestClient
from main import app

@pytest.fixture
def client():
    """In-process client -- no HTTP server, no network."""
    return TestClient(app)

# tests/contract/test_team.py
def test_get_team(client):
    resp = client.get("/v3/team/254")
    assert resp.status_code == 200
    data = resp.json()
    assert data["team"] == 254
    assert "name" in data
    assert isinstance(data["norm_epa"], (float, type(None)))

def test_get_team_years_sorted(client):
    resp = client.get("/v3/team_years?year=2024&metric=epa&limit=10")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 10
    epas = [d["epa"]["total_points"]["mean"] for d in data]
    assert epas == sorted(epas, reverse=True)
```

**Verification mode (`make verify`):** Runs the same contract tests against a live HTTP server. The target is selected via environment variable:

```bash
# Against a local dev server (http://localhost:8000)
make verify

# Against a staging server
STATBOTICS_URL=https://staging.statbotics.io make verify

# Against production
STATBOTICS_URL=https://api.statbotics.io make verify
```

The verification harness swaps the client fixture from `TestClient` to a `requests.Session` pointed at the target URL:

```python
# tests/contract/conftest.py
@pytest.fixture
def client():
    url = os.environ.get("STATBOTICS_URL")
    if url:
        return HttpClient(url)  # thin wrapper around requests.Session
    else:
        return TestClient(app)  # in-process, no network
```

This means every contract test is written once and works in both modes.

**Makefile targets:**

```makefile
test:                ## Run all local tests (unit + contract via TestClient, no external deps)
	poetry run pytest tests/ -m "not verify"

verify-local:        ## Run contract tests against local dev server (http://localhost:8000)
	poetry run pytest tests/contract/ --statbotics-url=http://localhost:8000

verify-prod:         ## Run contract tests against live production website
	poetry run pytest tests/contract/ --statbotics-url=https://api.statbotics.io

verify-staging:      ## Run contract tests against staging server for new backend
	poetry run pytest tests/contract/ --statbotics-url=https://staging.statbotics.io
```

`make test` always works without any third-party dependencies -- it's the default for local development. The three `verify-*` targets each hit a specific server and are never run as part of the default test suite.

**Coverage targets:**
- Every `/v3/*` endpoint (team, teams, year, team_year, team_years, event, events, team_event, team_events, match, matches, team_match, team_matches)
- Every `/v3/site/*` endpoint (teams/all, team/{num}, team/{num}/{year}, events/all, events/{year}, event/{id}, team_years/{year})
- Query parameter combinations: filtering (country, state, district, year), sorting (metric + ascending), pagination (limit, offset)
- Edge cases: non-existent team, future year, year 2021 (no data)

**PR scope:** ~800 lines. Pure additions, no changes to existing code.

### Testing Phase 2: Refactor for modularity, then test the calculation layer

**Goal:** Decouple the EPA model from SQLAlchemy so it can be tested with in-memory fixtures. This is the "refactor with a safety net" step -- Phase 1 unit tests protect the pure-math layer and the API contract tests catch any behavioral regression during refactoring.

**Refactoring steps (each a separate PR):**

**PR 3: Extract standalone dataclasses from ORM**

Currently `Match`, `TeamYear`, etc. are generated from ORM classes:
```python
# Current: dataclass depends on ORM
_Team = generate_attr_class("Team", TeamORM)  # inspects SQLAlchemy columns
class Team(_Team, Model): ...
```

Refactor to: define plain dataclasses independently, then have the ORM map to them:
```python
# New: standalone dataclass
@dataclass
class Team:
    team: int
    name: str
    country: Optional[str]
    ...

# ORM wraps the dataclass (or maps to same columns independently)
class TeamORM(Base):
    ...
    def to_model(self) -> Team: ...
```

This is a large but mechanical change. Every field already exists in the ORM -- we're just making the dataclass definition explicit rather than auto-generated. The ORM layer becomes a thin adapter.

**Tests to add with PR 3:**
- Verify all dataclass fields match ORM columns (automated schema comparison test)
- Verify `to_model()` / `from_model()` round-trips

**PR 4: Decouple EPA model from db.models imports**

After PR 3, the EPA model imports standalone dataclasses instead of ORM-generated ones. The EPA model becomes testable without SQLAlchemy:

```python
# Before: from src.db.models import Match, TeamYear, Year
# After:  from src.models.types import Match, TeamYear, Year  (standalone dataclasses)
```

Also extract `Year.get_mean_components()` and similar methods that encode data access patterns into plain parameters:

```python
# Before: init_epa depends on year.get_mean_components()
def get_init_epa(year: Year, ...) -> SkewNormal

# After: pass components directly
def get_init_epa(year_num: int, mean_components: np.ndarray, score_sd: float, ...) -> SkewNormal
```

**PR 5: Unit tests for EPA model and pipeline calculations**

Now that the EPA model is decoupled, add comprehensive tests:

5. **`tests/models/epa/test_init.py`** -- EPA initialization
   - Initialize from no prior years (rookie team)
   - Initialize from one prior year
   - Initialize from two prior years (weighted mean reversion)
   - Verify `epa_start` values match expected for known inputs

6. **`tests/models/epa/test_main.py`** -- Full EPA model
   - Construct model, call `start_season()` with fixture data
   - Process a known sequence of matches, verify predictions and updated ratings
   - Test `predict_match()` output against known values
   - Test `attribute_match()` error calculation
   - Test `update_team()` SkewNormal update
   - Test elimination match weighting (`ELIM_WEIGHT = 1/3`)
   - Regression test: process an entire historical event's matches, compare output to known-good EPA values from the current production database

7. **`tests/data/epa/test_calc.py`** -- Pipeline EPA step
   - Feed a year's worth of fixture data through `process_year()`
   - Verify all output objects (matches, team_matches, team_events, team_years) have expected EPA values
   - Test incremental processing: process N matches, checkpoint, process N+M matches from checkpoint, verify same result as processing all N+M from scratch

8. **`tests/data/test_avg.py`**, **`test_wins.py`** -- Pipeline calculation steps
   - Feed fixture data, verify computed averages and win records

**PR scope:** PR 3 is ~1000 lines of mechanical refactoring. PR 4 is ~200 lines of import/signature changes. PR 5 is ~1500 lines of new tests.

### Testing Phase 3: Shadow comparison harness

**Goal:** Provide an automated way to compare old and new backends side by side during the architectural migration. This is built after the refactoring is complete but before the backend is swapped out.

**PR (part of Round 3):** Shadow comparison test harness

A test that runs the same request against two servers and compares responses field by field:

```python
# tests/shadow/test_shadow_compare.py
OLD_URL = os.environ["OLD_BACKEND_URL"]
NEW_URL = os.environ["NEW_BACKEND_URL"]

def compare_responses(path):
    old = requests.get(f"{OLD_URL}{path}").json()
    new = requests.get(f"{NEW_URL}{path}").json()
    diff = deep_compare(old, new, float_tolerance=0.01)
    assert not diff, f"Mismatch on {path}: {diff}"

def test_all_endpoints():
    for path in generate_all_endpoint_paths():
        compare_responses(path)
```

This always requires two live servers (comparing old vs. new), so it has its own makefile targets:

```makefile
shadow-local:        ## Compare local new backend against production
	poetry run pytest tests/shadow/ --old-url=https://api.statbotics.io --new-url=http://localhost:8000

shadow-staging:      ## Compare staging new backend against production
	poetry run pytest tests/shadow/ --old-url=https://api.statbotics.io --new-url=https://staging.statbotics.io
```

**PR scope:** ~300 lines (comparison framework + endpoint generator).

### Local test data bootstrap

Many tests beyond Phase 1 need real data -- the contract tests need a populated database to query, the performance benchmarks need historical Parquet files, and the shadow comparison needs a working local backend. Rather than mocking this or depending on a live connection to TBA or CockroachDB at test time, we build a self-contained local dataset once and use it for everything.

**`make bootstrap-data`:**

This is a one-time (or periodic) step that creates the full local test dataset:

1. **Snapshot production data from the live API.** Hit the production statbotics API (not TBA) to fetch the current state of all entities for every year. This avoids needing TBA credentials or a CockroachDB connection. The data is already computed -- we're just downloading it.

```python
# scripts/bootstrap_test_data.py
for year in range(2002, CURR_YEAR + 1):
    if year == 2021:
        continue
    teams = fetch(f"https://api.statbotics.io/v3/team_years?year={year}&limit=1000&offset=0")
    # paginate until exhausted...
    events = fetch(f"https://api.statbotics.io/v3/events?year={year}")
    matches = fetch(f"https://api.statbotics.io/v3/matches?year={year}&limit=1000&offset=0")
    # ... etc for team_events, team_matches
```

2. **Write historical Parquet files.** Convert each year's JSON responses to Parquet and store locally:

```
backend/test_data/
├── historical/
│   ├── 2002/
│   │   ├── matches.parquet
│   │   ├── team_matches.parquet
│   │   ├── team_years.parquet
│   │   ├── team_events.parquet
│   │   ├── events.parquet
│   │   └── year.json
│   ├── 2003/
│   │   └── ...
│   └── 2025/
│       └── ...
├── current/
│   └── snapshot.duckdb       # current year loaded into DuckDB
├── teams.parquet
└── manifest.json             # timestamp of when this dataset was built
```

3. **Build the current-year DuckDB snapshot.** Load the current year's data into an in-memory DuckDB, compute EPA state from the match sequence, and export the snapshot. This is the same data the production system has, just stored locally.

4. **Git-ignore the data, but check in the script.** The `test_data/` directory is in `.gitignore` (it's ~500 MB). The bootstrap script is checked in. A developer clones the repo, runs `make bootstrap-data` once, and has everything they need.

**Makefile:**

```makefile
bootstrap-data:      ## Download production data and build local Parquet + DuckDB test dataset
	poetry run python scripts/bootstrap_test_data.py

bootstrap-data-quick: ## Same but only current year + 2 recent historical years (for quick iteration)
	poetry run python scripts/bootstrap_test_data.py --years=2024,2025,2026
```

The full bootstrap takes a few minutes (rate-limited API calls for ~24 years of data). The quick variant grabs just enough for most development work.

**How other test targets use this data:**

- `make test` — contract tests via TestClient use the local DuckDB snapshot and Parquet files. The FastAPI app is configured to read from `test_data/` instead of GCS when `TEST_DATA_DIR` is set.
- `make verify-local` — local dev server started with `TEST_DATA_DIR=test_data/` serves from the local dataset.
- `make perf` — performance benchmarks run against the local dataset, no network needed.

This means after the initial `make bootstrap-data`, all local testing is fully offline. No TBA, no CockroachDB, no GCS.

### Testing Phase 4: Performance benchmarks

**Goal:** Before migrating production, verify that the new DuckDB backend meets performance targets on a local machine. If it's fast on a laptop with local Parquet files, it will be fast in production. This catches performance regressions before they reach users.

**PR (part of Round 3, after DuckDB read layer is built):** Performance benchmark harness

The benchmark starts a local dev server running the new DuckDB backend, then hammers it with concurrent requests across a realistic query mix. It measures throughput (queries/second) and latency (p50, p95, p99) over a fixed time window.

**Query mix:**

The benchmark defines a weighted mix of endpoint calls that reflects real usage:

```python
QUERY_MIX = [
    # Current year hot path (70% of traffic)
    (30, "/v3/team_years?year=2026&metric=epa&limit=50"),
    (15, "/v3/site/event/2026mitry"),
    (10, "/v3/matches?year=2026&event=2026mitry"),
    (10, "/v3/site/team/254/2026"),
    (5,  "/v3/team_year/2026/1678"),

    # Historical cold path -- Parquet reads (20% of traffic)
    (5,  "/v3/team_years?year=2019&metric=epa&limit=50"),
    (5,  "/v3/matches?year=2015&team=254"),
    (5,  "/v3/team_year/2018/1114"),
    (5,  "/v3/site/event/2019mitry"),

    # Cross-year queries -- multiple Parquet files (10% of traffic)
    (5,  "/v3/team/254"),           # all years for a team
    (3,  "/v3/team_events?team=1678"),
    (2,  "/v3/team_matches?team=254&year=2019"),
]
```

**Benchmark execution:**

1. Start the local DuckDB-backed server against the `test_data/` directory (created by `make bootstrap-data`)
2. Warm up: run each endpoint once to populate DuckDB's Parquet cache
3. Benchmark: spawn N concurrent workers (default: 10), each picking randomly from the weighted query mix, for a fixed duration (default: 30 seconds)
4. Report: total queries completed, queries/second, and p50/p95/p99 latency

```python
# tests/perf/test_benchmark.py
import asyncio, aiohttp, time, random

async def worker(session, base_url, query_mix, results, end_time):
    while time.monotonic() < end_time:
        path = random.choices(
            [q[1] for q in query_mix],
            weights=[q[0] for q in query_mix]
        )[0]
        start = time.monotonic()
        async with session.get(f"{base_url}{path}") as resp:
            await resp.read()
            results.append((path, resp.status, time.monotonic() - start))

async def run_benchmark(base_url, duration=30, concurrency=10):
    results = []
    end_time = time.monotonic() + duration
    async with aiohttp.ClientSession() as session:
        workers = [worker(session, base_url, QUERY_MIX, results, end_time)
                   for _ in range(concurrency)]
        await asyncio.gather(*workers)
    return results
```

**Performance targets:**

| Metric | Target | Notes |
|--------|--------|-------|
| Current year throughput | > 500 queries/sec | In-memory DuckDB, should be trivial |
| Current year p99 latency | < 50 ms | Single query, including HTTP overhead |
| Historical year throughput | > 100 queries/sec | After Parquet cache warm-up |
| Historical year p99 latency | < 200 ms | First access slower (Parquet download), subsequent fast |
| Cross-year query p99 latency | < 500 ms | Multiple Parquet files, predicate pushdown |
| Mixed workload throughput | > 200 queries/sec | Weighted mix at 10 concurrent workers |
| Error rate | 0% | No 500s under load |

These targets are conservative -- the actual system should exceed them significantly. The point is to catch "something is unexpectedly slow" rather than to tune for maximum performance.

**Makefile target:**

```makefile
perf:                ## Run performance benchmarks against local DuckDB backend (localhost:8000)
	poetry run pytest tests/perf/ --statbotics-url=http://localhost:8000 -s
```

This is separate from `make test` because performance benchmarks are inherently noisy and depend on the local machine's capabilities. They're run manually before migration, not as part of CI.

**PR scope:** ~400 lines (benchmark harness + query mix + reporting).

## Migration Plan

Migration is structured as a sequence of pull requests, ordered so that early PRs are safe, non-controversial improvements (testing, refactoring) and later PRs introduce the architectural changes. Each PR stands alone and delivers value independently. The testing phases above map directly to Rounds 1 and 2.

### Round 1: Testing (establishes credibility, zero risk)

Corresponds to Testing Phase 1. These PRs add test infrastructure and coverage without changing any existing behavior.

| PR | Description | Risk | Depends on |
|----|-------------|------|-----------|
| 1 | Test infrastructure + unit tests for pure math, TBA breakdown, TBA client | None -- pure additions | -- |
| 2 | End-to-end API contract tests against production | None -- read-only against prod | PR 1 |

### Round 2: Refactoring for modularity (safe with test coverage)

Corresponds to Testing Phase 2. These PRs restructure code boundaries without changing behavior. The unit tests from PR 1 and the API contract tests from PR 2 protect against regressions.

| PR | Description | Risk | Depends on |
|----|-------------|------|-----------|
| 3 | Extract standalone dataclasses from ORM | Low -- mechanical, behavior-preserving | PR 1, 2 |
| 4 | Decouple EPA model from db.models imports | Low -- import path changes only | PR 3 |
| 5 | Unit tests for EPA model and pipeline calculations | None -- pure additions | PR 4 |

### Round 3: New backend (the architectural change)

With comprehensive test coverage and clean module boundaries, the backend can be rebuilt. The shadow comparison harness (Testing Phase 3) and performance benchmarks (Testing Phase 4) are built as part of this round. Performance is validated locally before any production traffic is shifted.

| PR | Description | Risk | Depends on |
|----|-------------|------|-----------|
| 6 | Local test data bootstrap (`make bootstrap-data`) + Parquet export | None -- read-only, offline tool | PR 3 |
| 7 | DuckDB read layer (`src/db_duckdb/`) with same interface as `src/db/read/` | Low -- new code, not wired in | PR 3, 6 |
| 8 | Shadow comparison harness + shadow mode in API handlers | Low -- old path still primary | PR 2, 7 |
| 9 | Performance benchmark harness + local validation | None -- local only, not wired in | PR 6, 7 |
| 10 | Incremental EPA with checkpointing | Medium -- new write path | PR 4, 5 |
| 11 | Stale-while-revalidate refresh loop | Medium -- replaces data router | PR 10 |
| 12 | DuckDB primary: cut over read path | Medium -- traffic shift | PR 8, 9 (validated) |
| 13 | Remove CockroachDB, collapse to single Cloud Run service | High -- irreversible | PR 12 (proven) |

### Round 4: Frontend simplification

| PR | Description | Risk | Depends on |
|----|-------------|------|-----------|
| 14 | Remove GCS bucket fetch from frontend, always use API | Low | PR 13 |
| 15 | Simplify frontend caching (optional) | Low | PR 14 |

### Rollback strategy

- **PRs 1-5** (testing + refactoring): Revert the PR. No state changes.
- **PRs 6-9** (Parquet export, DuckDB read layer, shadow mode, perf benchmarks): Old backend is still primary. Disable shadow mode.
- **PRs 10-12** (new write path, cutover): CockroachDB is still running and receiving writes until PR 13. Roll back to CockroachDB reads by reverting the cutover.
- **PR 13** (remove CockroachDB): This is the point of no return. Only execute after running through at least one full event weekend on DuckDB-only. Keep a CockroachDB snapshot for 30 days as insurance.

## Edge Cases

### Match score corrections

TBA occasionally corrects scores after posting. The refresh loop detects this by comparing score fields (`red_score`, `blue_score`, breakdown fields) of existing matches against TBA data. If a correction is found, falls back to full-season recompute. This is rare (a few times per season). See "Score correction detection" above for the exact field list.

### Process restart during event

Loads snapshot from GCS (last good state). First request triggers a refresh that catches up on missed matches. Gap is at most a few minutes of data.

### Snapshot upload failure or corruption

The snapshot upload to GCS uses an atomic write pattern: upload to a temporary object (`current/snapshot.duckdb.tmp`), then copy to the final path (`current/snapshot.duckdb`). GCS object copies are atomic. If the process crashes mid-upload, the temp file may be partial but the previous good snapshot at the final path is untouched.

On cold start, if the snapshot fails to load (corrupt file, missing file), fall back to a full-season rebuild from TBA data. This is the same as the "season start" path and takes ~30 seconds. The `manifest.json` file stores a version counter and checksum; cold start validates the snapshot checksum before loading.

### Season start (no checkpoint)

First update processes the full year from scratch and creates the initial checkpoint. Same as today's `reset_curr_year`.

### TBA API outage

If TBA returns errors instead of data, the refresh logs the error and releases the lock. Readers continue serving the last known good state. The next poll interval triggers another attempt.

### Cloud Run scale-to-zero

Cold start adds 2-3 seconds for snapshot download. Mitigate by setting minimum instances to 1 during event season.
