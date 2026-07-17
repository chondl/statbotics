# state-snapshot progress

Branch: `state-snapshot` (worktree `.worktrees/state-snapshot`), base `fork/blob-gc` (0c4388f).
Draft PR target: `chondl/statbotics` base `blob-gc` head `state-snapshot`. NEVER origin.

## Design
- Snapshot blob `state/snapshot.<year>` = full in-memory state (objs 6-tuple + teams),
  json+zlib, deterministic (sorted by pk), enum fields coerced to members on load so
  snapshot-loaded objects are byte-identical to DB-loaded objects.
- Cycle start: load state from snapshot; fall back to DB read when no snapshot.
- Publish blobs from in-memory state; cross-year enrichment DB reads best-effort.
- Snapshot + blob publish = source of truth; DB diff-upsert moved AFTER, non-fatal.
- Cross-year EPA seed read made best-effort (stays on DB; degrades on outage).

## Status
- [x] Explore architecture
- [x] Worktree created
- [x] snapshot.py module
- [x] storage.write_objs DB-independent publish
- [x] data/main.py wiring (load/fallback, snapshot write, non-fatal DB write)
- [x] Verify (a) byte-identical replay: 0/3950 mismatches
- [x] Verify (b) DB-down headline test: cycle OK 18.4s, manifest+snapshot advanced, DB write logged+skipped
- [x] Verify (c) cold start fallback (no snapshot -> DB read path)
- [x] Verify (d) timings: Read Snapshot 1.4s vs Read Objs+Load Teams 2.7s
- [x] Verify (e) smoke 10/10
- [x] Verify heal: deleted 25 team_years, next DB-up cycle restored them
- [x] Push + draft PR

Draft PR: https://github.com/chondl/statbotics/pull/9 (base blob-gc, head state-snapshot, fork only)

Commits (base fork/blob-gc 0c4388f):
- 94bf753 Add snapshot serialization for pipeline state
- b8229eb Publish blobs from in-memory state, tolerate DB outage
- c2d1ef8 Load state from snapshot, move DB writes off the hot path

## Notes
- Rig restored: state/ blobs deleted, crdb+fake-gcs up, smoke 10/10.
- Byte-identical required normalizing read_objs to pk-sorted order (matches snapshot).
- Healing preserved by diffing DB write vs a best-effort fresh DB read (not snapshot).
- Smoke transiently showed stale-pooled-conn 500s right after crdb restart; self-heals on retry.
