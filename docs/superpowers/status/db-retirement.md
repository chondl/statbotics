# db-retirement progress

Branch: `db-retirement` (worktree `.worktrees/db-retirement`), base `fork/duckdb-api` (9a4eb09).
Draft PR target: `chondl/statbotics` base `duckdb-api` head `db-retirement`. NEVER origin.

## Scope (close PR #10 cutover gap)
1. Last pipeline DB reads -> blobs/parquet:
   - etags already in snapshot tuple (objs[5]); verified process_year_tba reads
     from the passed list, never the DB. DONE (no change needed).
   - cross-year EPA seed reads (prior-year team_years) -> parquet via db_duckdb,
     graceful fallback (empty -> rookie seeding) when prior-year parquet absent.
   - PR #9 best-effort healthy-cycle DB read: skipped in db-less mode.
2. /v3/site db-less: site read fns already route through src/api facade via
   get_*_cached (PR #10). Only get_noteworthy_matches / get_upcoming_matches
   read the DB directly -> add duckdb variants + src/site/backend.py facade.
3. One flag DISABLE_DB: no engine/session constructed; pipeline skips DB
   upsert; /v3 + /v3/site serve from DuckDB/parquet. Default unchanged.

## Design decisions
- DISABLE_DB env flag (src/constants.py, mirrors PROD). Implies duckdb serving.
- src/db/main.py: engine/Session = None under DISABLE_DB (literal "no engine or
  session constructed"; create_engine is otherwise lazy anyway).
- src/data/backend.py: read facade (get_teams/team_years/team_events) dispatch.
- refresh_teams: DB-reconciliation maintenance op, no-op db-less (team metadata
  flows through snapshot each cycle) -> early return.
- Non-goals per PR body: incremental EPA, stale-while-revalidate. Full replay.

## Status
- [x] Worktree created (base fork/duckdb-api 9a4eb09)
- [x] constants + db/main flag (engine/Session = None db-less)
- [x] serve /v3 + /v3/site db-less (facade + duckdb noteworthy/upcoming)
- [x] pipeline db reads -> parquet, writes skipped db-less
- [x] storage.write_objs site blobs regenerate db-less (facades)
- [x] Rig verify: cold start, CRDB stopped, DISABLE_DB, 3+ cycles + smoke 10/10
- [x] Rig verify: parquet cross-year seed parity vs DB-seeded (0.00e+00, 60 teams)
- [x] Rig verify: flip DB back on, heal from snapshot (3699 -> 3724), smoke 10/10
- [x] Default mode unchanged: DB-on smoke 10/10
- [x] Push + draft PR #11
- [x] migration-overview Stack PR E line

Draft PR: https://github.com/chondl/statbotics/pull/11 (base duckdb-api, head db-retirement, fork only)
Diff: +244/-37, 11 files.

Commits (base fork/duckdb-api 9a4eb09):
- ececd4c Add DISABLE_DB flag; construct no engine or session db-less
- ca7ce4b Serve /v3 and /v3/site from DuckDB when DISABLE_DB
- c6655d2 Move last pipeline DB reads to Parquet, skip DB writes db-less
- 4e4952b Regenerate bucket-first site blobs db-less; tolerate absent Parquet year

## Notes
- Rig RESTORED: my servers stopped, parquet/ (6) + state/ (1) blobs deleted,
  orphan 2025 team_years cleaned, rig-local servers back on 8000/8001, smoke 9/9
  DB mode. crdb has 3724 2026 team_years.
- Check-semantics: smoke labels /v3/team + /v3/site/team_years "db-backed"; db-less
  they serve from DuckDB/Parquet (label historical). No check needs CockroachDB.
- DISABLE_DB matches PROD convention: value must be exactly "True".
- storage.write_objs also pre-renders bucket-first blobs via DB reads -> routed
  through src/data/backend + src/site/backend so the bucket frontend stays fresh.
- db_duckdb _query returns [] on duckdb "No files found" (absent year = no rows,
  matching DB), so cross-year seed of a missing prior year -> rookie seeding
  cleanly (no traceback).
</content>
</invoke>
