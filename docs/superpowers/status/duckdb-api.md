# duckdb-api progress

Branch: `duckdb-api` (worktree `.worktrees/duckdb-api`), base `fork/state-snapshot` (c2d1ef8).
Draft PR target: `chondl/statbotics` base `state-snapshot` head `duckdb-api`. NEVER origin.

## Design
- **Parquet publish target**: each cycle export current-year tables
  (team_years, events, team_events, matches, teams, year) to
  `parquet/{year}/{table}.parquet`. Built from the same in-memory objs as the
  snapshot/blobs. Atomic tmp+copy write; content-gated by md5 (skip unchanged).
  Historical backfill (`reset_all_years`) exports each year the same way.
- **DuckDB read layer** `src/db_duckdb/`: same interfaces as `src/db/read` used by
  `/v3` routers. In-process DuckDB over a local cache of the parquet blobs
  (generation-synced). Column whitelist for sort metric; parameterized filters.
- **Selection**: `src/api/backend.py` facade dispatches on `API_BACKEND=duckdb`
  (default = relational DB). Only the 6 `/v3` public routers switch.

## Status
- [x] Explore architecture
- [x] Worktree created (duckdb 1.5.4 + pyarrow 20 installed in rig venv)
- [x] schema + parquet writer (pyarrow columnar; ~2.3s/cycle, content-gate 6/6 stable)
- [x] db_duckdb read layer (local parquet cache, generation-synced)
- [x] api backend facade wiring (API_BACKEND flag; 6 routers switched)
- [x] Parquet publish wired into pipeline (not DISABLE_GCS; historical via reset_all_years)
- [x] Rig verify: parity 0/67684 field mismatches; smoke 9/9 duckdb mode; latency swept
- [x] Push + draft PR

Draft PR: https://github.com/chondl/statbotics/pull/10 (base state-snapshot, head duckdb-api, fork only)

Commits (base fork/state-snapshot c2d1ef8):
- 4c2798d Add Parquet/DuckDB table schema introspection
- 14763c0 Publish current-year tables to Parquet each cycle
- e334d09 Add DuckDB read layer over Parquet blobs
- 9a4eb09 Serve /v3 from DuckDB behind API_BACKEND flag

## Results
- Parity: 67,684 fields compared, 0 field-level mismatches. Only diffs = equal-sort-key
  row order under paginated ORDER BY without tiebreaker (proven tiebreak-only via
  (metric,pk) re-sort; norm_epa 2490/2944 tied). Full sets identical (2944==2944).
- Latency: DuckDB faster on nearly all classes; team_years pagination p95 322ms->43ms.
  team_year point lookup marginally slower (full-file scan). Parquet write ~2.3s.
- Smoke 9/9 in duckdb mode (consistency max EPA diff 0.0000).
- Diff: +594/-6, 14 files.

## Notes
- Enums are `str, Enum`; JSON cols = team_year.matches, match.pre_epas/epas.
- Rig-only verification. NEVER touched staging (historical-data backfill running).
- Rig RESTORED: worktree servers (8010/8011) stopped, parquet/ blobs deleted,
  original :8000/:8001 healthy, smoke 9/9 DB mode.
- Circular-import fix: PARQUET_PREFIX lives in schema.py; db_duckdb.main imports
  _bucket lazily (storage.py pulls site->api).
- pyarrow used for the writer (fast columnar); executemany-based duckdb writer was
  100s/cycle, replaced.
