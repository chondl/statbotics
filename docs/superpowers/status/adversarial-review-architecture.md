# Adversarial review — architecture migration stack (PRs #2, #7, #9, #10, #11)

Reviewer pass date: 2026-07-10. Scope: CockroachDB-serving → static-file/DuckDB
serving arc. Read-only on all branches; no writes to staging; rig state untouched
(no cycles run). Severity: P0 broken / P1 wrong-or-severe / P2 degraded / P3
polish/latent. Each finding tagged NEW vs KNOWN. KNOWN items (per PR bodies +
status docs) are NOT re-argued; a confirmation count is at the bottom.

Worktree paths referenced are the final-state branch `db-retirement`
(`.worktrees/db-retirement`) unless noted; the same code originates in the PR that
introduced it.

---

## NEW findings

### P2 — `write_parquet` can silently clobber every frontend blob reference in the manifest
`backend/src/google/parquet.py:59-77`

`write_parquet` does read-modify-write on the SAME `manifest.json` that
`storage._publish` wrote moments earlier in the same cycle:

```python
manifest = read_manifest() or Manifest()      # line 59
blobs = dict(manifest.blobs)                   # line 60
... # add parquet/* keys
if changed:
    manifest.blobs = blobs
    write_manifest(manifest, bucket)           # line 77
```

`read_manifest()` swallows all exceptions and returns `None` on any transient GCS
read error (`storage.py:80-84`). When that happens, `or Manifest()` yields a FRESH
empty manifest, `blobs` starts `{}`, and the code writes a manifest containing
**only `parquet/*` keys** — erasing every `team/*`, `event/*`, `team_years/*`,
`teams/all`, etc. reference. The frontend then resolves nothing from the manifest
and falls through to the `?t=` legacy/hist path (`storage.tsx:51-55`) and finally
the backend API. If this coincides with the DB/backend outage the whole migration
is designed to survive, the site goes dark until the next data-change cycle
re-renders all site blobs (`_publish` re-adds every rendered logical path, so it
self-heals in one cycle). Failure is SILENT — no error logged, `write_parquet`
proceeds normally. Would be P1 if it lands during the target outage scenario.
NEW.

### P2 — db-less DuckDB cold start crashes (500) on team lookups when no Parquet exists
`backend/src/db_duckdb/main.py:110` (`_teams_source` → `max(_years(base))`)

`_teams_source` is `max(_years(base))`; `_years` returns `[]` when the cache dir
has no `parquet/` tree. `max([])` raises `ValueError` (confirmed empirically) — an
uncaught 500 from `get_team` / `get_teams`. This is exactly the first-ever db-less
deploy path: empty bucket → `_sync` fails to reload the manifest → returns an empty
gen dir (`main.py:59-62`) → `_years` `[]` → crash. Inconsistent with sibling
entities: `get_year`/`get_years`/`get_events` glob `*/…parquet`, hit DuckDB "No
files found", and return `[]` gracefully (`main.py:146-147`). There is no bootstrap
path — `/v3/team/*` and `/v3/teams` 500 until the first pipeline cycle writes
Parquet (and even then only after the 30s `_sync` TTL from the failed cold read
elapses). NEW.

### P2 — "one atomic manifest" is actually two non-atomic manifest writes per data-change cycle
`backend/src/data/main.py:87` vs `:102`; `backend/src/google/parquet.py:75-77`

The pipeline writes the manifest twice on any cycle where data changed:
`write_objs_storage` (`_publish` → `write_manifest`, site blobs + carried-forward
OLD parquet keys) then `write_parquet` (`write_manifest` again, + NEW parquet
keys). Consequences: (a) a DuckDB `_sync` landing between the two writes serves the
PREVIOUS cycle's Parquet while the frontend serves the current cycle — the /v3 API
and the website disagree by one cycle transiently, undercutting the
migration-overview "one atomic manifest … blob == API verified to 0.0000" claim;
(b) a crash between line 87 and line 102 strands the DuckDB-served API a full cycle
behind the frontend until the next data-change cycle; (c) doubles churn on the
one 60s-TTL object clients re-poll. The parquet-atomicity doc's premise ("parquet
folded into the manifest written LAST") holds only WITHIN `write_parquet` — it is
not the same write as the site-blob manifest. NEW.

### P2 — db-less cross-year EPA seed degrades silently to rookie values if prior-year Parquet is absent
`backend/src/data/main.py:59-66` + `backend/src/db_duckdb/main.py:146-147`

`process_year` seeds cross-season EPA from `get_team_years_db(year=prior)`; in
db-less this routes to DuckDB, and a missing prior-year Parquet file returns `[]`
("No files found" swallowed at `main.py:146-147`). The whole read is wrapped in
`try/except … traceback.print_exc()` (`main.py:61-66`) so an empty result →
`all_team_years` empty → every team rookie-seeded. Unlike the DB (which always
holds full history), db-less has prior years ONLY if backfilled to Parquet. A
partial/forgotten backfill therefore produces SILENTLY WRONG EPA seeds (everyone
regresses to the rookie mean) with no error — the "graceful fallback to rookie
seeding" masks a misconfiguration. The db-retirement 0.00-parity evidence held
only because prior-year Parquet had been backfilled. NEW.

### P2 — snapshot schema version is written but never validated; missing fields silently become None
`backend/src/google/snapshot.py:22,63,78-90` + `backend/src/db/models/main.py:16`

`SNAPSHOT_SCHEMA = 1` is embedded in the payload (line 63) but `deserialize`
(78-90) never reads or checks `payload["schema"]` (grep-confirmed: only two
references, both writes). So the "versioned snapshot" has no version gate in either
direction. Worse, `Model.from_dict` is
`{k: dict.get(k, None) for k in cls.__slots__}` — any field absent from an old
snapshot is set to `None`, NOT its attr default. A deploy whose ORM added a column
reads the pre-deploy snapshot and gets `None` for that column; because TBA etags
live in the same snapshot, unchanged rows are never re-fetched, so the new column
stays `None` indefinitely for existing objects (until a full reset). No fail-safe
to the DB path — the load "succeeds" with silently-nulled data. NEW.

### P3 — migration-overview overstates staging coverage of Stack C/D/E
`docs/superpowers/deliverables/migration-overview.md:3`

Line 3 frames the whole thing as "verified on a local rig … and running live at
[staging] … deployed from the merged stack." Verified against the staging worktree:
staging contains bucket-first (#2), blob-gc (#7), epa-consistency (#1),
match-page-fixes (#5), qa-fixes (#6), sos-sim-fix (#8), postgres-compat — but has
**no `snapshot.py`, no `parquet.py`, no `src/db_duckdb/`, and no `DISABLE_DB`**.
state-snapshot (#9), duckdb-api (#10), and db-retirement (#11) are RIG-ONLY (their
own status docs say "NEVER touched staging"). The Stack C/D/E "Evidence" bullets
are honestly rig-worded, but the umbrella sentence conflates them with "running
live at staging." A maintainer who inspects staging and finds no DuckDB/snapshot/
db-less code will read the top-line framing as inflated. NEW.

### P3 — `team/{num}` blobs lag the `team_years` list by one cycle (PR #9 write-ordering)
`backend/src/google/storage.py:181-191`

PR #9 moved persisted (DB, and now Parquet) writes to AFTER publish. But
`write_objs` still builds per-team pages from `get_team_years_db` (line 181), which
reads the PREVIOUS cycle's persisted state, while `team_years/{year}` (line 138)
uses the current in-memory objs. So a team's current-year EPA on its own team page
is one cycle (~13s) behind the /teams list right after a match — a smaller, faster
re-run of the therekrab "team page vs list disagree" class that PR #1 set out to
fix, and a transient violation of the "blob == API to 0.0000" claim. NEW.

### P3 — two concurrent publishers are undetected; shared snapshot tmp key; last-writer-wins regression
`backend/src/google/snapshot.py:96`; `backend/src/google/storage.py:225-228`

Nothing detects two publishers (double scheduler fire, or two Cloud Run revisions
taking the data trigger). `write_snapshot` uses a SHARED tmp key
`state/snapshot.<year>.tmp` (line 96), so concurrent publishers race on it; the
manifest is plain last-writer-wins (`_publish`), so a slower/staler publisher can
regress the site by a cycle. Objects are content-addressed so no torn set, and it
self-heals, but it is entirely un-alerted. NEW.

### P3 — a single failed manifest fetch degrades ALL client requests to the uncached legacy path for 60s
`frontend/src/api/storage.tsx:32-39,45-55`

`getManifest` memoizes the manifest promise for 60s including a resolved-`null`
result. One failed/blipped `manifest.json` fetch → `getManifest` returns `null` for
a full minute → every `resolveBucketUrl` falls to
`${BUCKET_URL}/${logicalPath}?t=${Date.now()/1000/60}` (line 55), a per-minute
cache-buster that defeats CDN/browser caching — during exactly the degraded
conditions the bucket-first design is meant to ride out. Amplifies the KNOWN `?t=`
latent path (qa-adversarial) by pinning it on for 60s. NEW.

### P3 (latent) — `toLogicalPath` only rewrites the first `?`/`&`
`frontend/src/api/storage.tsx:41-43`

`apiPath.replace("?", ".").replace("&", ".")` uses string args, replacing only the
FIRST occurrence of each. All current manifest keys have ≤1 `&`, so it works today,
but any future 3-query-param blob key silently mis-resolves (manifest miss → legacy/
backend fallback). Fragile; note for season-prep when new parameterized blobs are
added. NEW (latent).

---

## Concurrency / crash-point enumeration (for the record; no NEW defect beyond the above)

Cycle write order: snapshot (`main.py:84`) → site blobs+manifest#1 (`:87`) → DB
upsert (`:90`, non-fatal) → parquet+manifest#2 (`:102`).

- Crash after snapshot, before manifest#1: next cycle loads snapshot (source of
  truth), recomputes, republishes. Self-heals. OK.
- Crash after manifest#1, before DB: DB one cycle behind; heals via next diff-upsert
  (KNOWN best-effort heal-diff trade).
- Crash after manifest#1/DB, before parquet#2: DuckDB API one cycle behind frontend
  (see P2 double-manifest finding).
- GC vs publish: 48h youth grace >> any Cloud Run↔GCS clock skew (seconds) and >>
  60s client/CDN TTL; grace absorbs the skew attack. Not a defect.
- DuckDB gen-dir growth: `_sync` rmtree's the gen-before-prev each swap
  (`main.py:85-92`), retaining current+prev — cleanup IS implemented, at most 2 dirs.
  In-flight query vs swap is protected by the 30s sync TTL plus one-gen retention;
  only a >30s query spanning two swaps is at risk (unlikely at 13s cycles).
- SQL injection: `metric` is whitelisted against the column set (`main.py:129`);
  all interpolated identifiers/order-exprs are hardcoded; year is an int; filter
  values are parameterized (`?`). Whitelist coverage is complete. Not a defect.

---

## KNOWN items confirmed (not re-reported)

Confirmed present in code, already documented in PR bodies / status docs:
1. Snapshot write cost (~9s prod / timing delta) — state-snapshot doc.
2. PR #9 best-effort healthy-cycle DB read for heal-diff — `main.py:92`.
3. Legacy unversioned paths still dual-written (retirement deferred) —
   `_publish` legacy_uploads.
4. team_year point lookup slower on DuckDB (full-file scan) — `_one`→`_query`.
5. Tie-break row-order nondeterminism in paginated sorts (ORDER BY metric, no pk
   tiebreak) — `_query:134-136`.
6. `?t=` cache-buster latent legacy path — qa-adversarial (amplified by P3 above).
7. Cloudflare 4h TTL-floor gotcha — cf-blob-proxy doc (not re-verified).

Count: 7 known items confirmed.
