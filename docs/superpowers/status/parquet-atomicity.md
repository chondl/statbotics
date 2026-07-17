# parquet-atomicity progress

Fix: Parquet read/write set is not cross-table atomic on the duckdb-api branch
(PR #10). Parquet written at FIXED paths, DuckDB refreshes per-file by generation
-> a reader syncing mid-publish can join matches (run N+1) with events (run N).
The relational path it replaces writes all tables in one transaction. Fix the set
atomicity.

Branch: `duckdb-api` (PR #10), worktree `.worktrees/duckdb-api`. Then rebase
`db-retirement` (PR #11) onto the new head.

## Design chosen
- Content-address parquet objects under the `v2/` prefix the reference-aware GC
  already scans: `v2/parquet/{year}/{table}.parquet.{digest}` (immutable, long
  Cache-Control), digest = sha256[:12] via `publish.content_hash`.
- Fold the parquet set into the EXISTING `manifest.blobs` map (logical key
  `parquet/{year}/{table}.parquet`). Written LAST, gzip, 60s TTL. Reused rather
  than a new `parquet` field because it is the smallest diff that achieves set
  atomicity: `plan_uploads` already carries `dict(prev.blobs)` forward, and GC
  already keeps `set(manifest.blobs.values())` under `v2/` -> zero change to
  publish.py planning and zero change to GC scope. Frontend resolves `blobs` by
  exact key lookup, so the extra `parquet/*` entries are inert there.
- DuckDB read layer resolves the whole set from ONE manifest fetch per sync:
  version the local cache dir per manifest generation, materialize the referenced
  parquet objects into it (hardlink-reuse unchanged objects from the prior gen
  dir), then atomically swap `_current_dir`. Each query captures `base = _sync()`
  once and threads it into every `_source(...)` so a query never spans two gen
  dirs. Old gen dir retained one generation for in-flight queries.
- Content-gating preserved: unchanged table -> `manifest.hash_for(logical)` equals
  the fresh digest -> no upload, key kept (copy-on-write, steady-state 0 uploads).

## Status — DONE (2026-07-10)
- [x] Code: parquet.py content-addressed publish + manifest update
- [x] Code: db_duckdb read layer manifest-based atomic sync (base threaded per query)
- [x] duckdb-api commit d9420af, pushed fork/duckdb-api (fast-forward on 9a4eb09)
- [x] db-retirement rebased onto d9420af (917723b), join reads adapted to base-threaded
      source, force-pushed fork/db-retirement
- [x] PR bodies updated (#10 addressing/atomicity/GC, #11 join-atomicity + diff stat)

## Verification (rig, fake-gcs + full 2026; RESTORED after)
- (a) Torn-set: interrupted publish (new matches+events objects uploaded, manifest
  NOT written) -> reader serves OLD coherent set for BOTH tables (red_score=469,
  name='Rocket City Regional'); completing the manifest write flips reader
  atomically to the new set (9999 / SENTINEL_TORN). No cross-table tear.
- (b) duckdb mode: smoke 9/9, consistency probe max diff 0.0000; 8-endpoint
  DB-vs-duckdb parity sweep all MATCH.
- (c) db-less (DISABLE_DB): smoke 9/9; noteworthy join = 6 groups/30 rows, upcoming
  join executes (0 = complete season), absent prior-year read -> [] cleanly.
- (d) Steady-state: 2 cycles, 6 parquet objects identical (0 new uploads).
- (e) GC: 6 manifest-referenced parquet KEPT, planted superseded parquet orphan
  identified for deletion (dry-run scanned 4616, kept 3956, deleted 660).
- Historical backfill addressing: synthetic 2025 export accumulated into the same
  manifest alongside 2026; duckdb resolved years [2025, 2026] from one manifest.
- Rig RESTORED: my servers stopped, 6 parquet objects + manifest parquet entries
  deleted, rig-local DB-mode 8000/8001 healthy, DB-mode smoke 9/9.

## Coordinator addendum: int32 time-underflow hazard (2026-07-10)
historical-data found match/event/team_event `time` (+ match `predicted_time`) are
ORM Integer (int32) and TBA ~1900 placeholder timestamps (-2208988800) underflow
int32 (fix Integer->BigInteger going to postgres-compat, OUTSIDE this stack).
Parquet path checked: ALREADY IMMUNE by design — `ARROW_TYPES["int"] = pa.int64()`
maps every SQLAlchemy Integer-family column to int64, and `BigInteger` subclasses
`Integer` so schema.py `_kind` yields "int" either way (no drift when the ORM fix
lands; verified `issubclass(BigInteger, Integer) == True`). No code change needed.
Empirical proof on BOTH branches (duckdb-api and rebased db-retirement): synthetic
match row with time=-2208988800, predicted_time=-2208988740 round-tripped
writer -> Parquet (physical type int64 confirmed via pyarrow schema) -> DuckDB
read_parquet -> from_row model, values exact. Hazard + handling noted in PR #10
body (schema introspection paragraph). Verification rig-only (fake-gcs); staging
never read — its DB/bucket are mid-backfill and not representative.

## Design chosen (recap)
Folded parquet into the existing `manifest.blobs` map with objects under `v2/`
(smallest diff: no publish-planning or GC-scope change; frontend does exact key
lookups so `parquet/*` entries are inert). Reader resolves the whole set from one
manifest fetch into a per-generation cache dir, atomic dir swap, base captured once
per query. Alternatives (separate manifest object; separate `parquet` field)
rejected as larger diff for no atomicity benefit.

## Review-fix update (2026-07-10, review-fixes agent)
The original design still wrote the manifest TWICE per current-year cycle
(`storage.write_objs` -> site manifest, then `write_parquet` -> a second manifest
that re-read and re-wrote it). Adversarial review A3 flagged that the "written
LAST / atomic" claim held only WITHIN `write_parquet`, not across the two writes,
so a DuckDB sync between them served last cycle's Parquet while the frontend served
this cycle, and A1 flagged that `read_manifest() or Manifest()` could clobber every
site-blob ref on a transient read failure.

Fix now in place:
- `parquet.build_parquet_uploads()` only serializes tables (no upload/manifest).
- For the current-year cycle, `storage.write_objs` takes the serialized parquet and
  folds it into its ONE `plan`/manifest, written exactly once, last. Verified on the
  rig: one `write_manifest` call carrying both site (3950) and parquet (6) entries.
- Historical years keep the standalone `write_parquet` (no co-occurring site
  publish), now A1-safe: it `read_manifest()`s and ABORTS+logs if unreadable instead
  of `or Manifest()`-fabricating an empty one.
- Injection test: with `read_manifest` forced to return None during a current-year
  cycle, the written manifest still carries all 3950 site refs (no clobber); the
  historical writer skips (0 manifest writes).
