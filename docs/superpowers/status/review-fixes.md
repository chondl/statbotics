# Review fixes — final status (2026-07-10)

All material findings from both adversarial reviews fixed on the owning branch,
cascaded through the stack, verified on the rig, force-pushed to fork, and every
affected PR body corrected. Rig state restored (pytest uninstalled, no writes to
site_dev_v1, temp bucket cleaned, smoke 9/9).

## Per-finding outcome

| ID | Branch(es) | Outcome |
|----|-----------|---------|
| F1 | epa-consistency (#1); bucket-first (#2) + state-snapshot (#9) | FIXED. Event blobs gate on event-specific content only; year drift no longer republishes all 215. Measured 215→0. |
| F2 | epa-consistency (#1); tests (#4) | FIXED. `nan_safe_eq` DB+blob gate. NaN row 1→0. Stack DB gate stayed `str()` (already NaN-stable) — documented, no change needed there. |
| F3 | epa-consistency (#1); verified state-snapshot (#9) | FIXED on #1 (consume upload results + storage-before-DB). Verified #9 already orders storage→DB and `_publish` consumes `executor.map`; self-heals via snapshot. |
| F4 | epa-consistency (#1) | FIXED. CUTOFF 250→200 (worst-case-safe, 200×69KB≈13.5MiB<16MiB). |
| F5 | epa-consistency (#1) | FIXED. Deferred match blanks red/blue score (≤5 lines). |
| F6 | epa-consistency-tests (#4) | FIXED. `test_nan_stability.py`, 19/19 pass. |
| A1 | duckdb-api (#10) | FIXED. No `read_manifest() or Manifest()` clobber; historical writer aborts+logs on unreadable manifest. |
| A2 | duckdb-api (#10); doc db-retirement (#11) | FIXED. `_teams_source` glob fallback (no max([]) crash); bootstrap order documented. |
| A3 | duckdb-api (#10) | FIXED. Current-year cycle folds parquet into ONE manifest write, last. Historical years write their own (rare bulk path). |
| A4 | db-retirement (#11) | FIXED. Loud warning + `/info` `DB_LESS_SEED_INCOMPLETE` flag; backfill-first documented. Also decoupled historical parquet from DISABLE_DB. |
| A5 | state-snapshot (#9) | FIXED. Schema validated on read; falls back to DB path on mismatch/corruption. |
| A6 | state-snapshot (#9) | FIXED. Snapshot tmp key carries pid+uuid. |
| A7 | bucket-first-serving (#2) | FIXED. Manifest null result not memoized 60s; `toLogicalPath` regex replaceAll. |
| team-blob lag P3 | state-snapshot (#9) | FIXED (small). Team pages use in-memory current-year rows. |
| migration-overview P3 | — | No change: line 3 already distinguishes rig-only stack PRs honestly. |
| F7 | — | SKIPPED per instructions (documented, low-confidence). |

## Measured evidence

- **F1 (rig, seeded 2026):** perturb year stats only, event content identical →
  OLD gate 215 event uploads, NEW gate **0**. One match changed → **1**. Holds on
  both epa-consistency (legacy overwrite gate) and the stack (content-addressed +
  event gate).
- **F2 (rig):** identical-NaN team_event → OLD attrs-`!=` DB gate writes **1** row
  every cycle, NEW `nan_safe_eq` writes **0**; event blob uploads 0.
- **A3 (rig):** one current-year cycle → **1** `write_manifest` call carrying both
  site (3950) and parquet (6) entries.
- **A1 (rig):** `read_manifest` forced None mid-cycle → manifest still carries all
  3950 site refs (no clobber); historical writer skips (0 writes).
- **A2 / db-less (rig, isolated temp bucket):** cold `get_team(254)` → None (no
  crash); after one folded cycle DuckDB serves 254=The Cheesy Poofs, year 2026,
  team_year epa from Parquet.
- **deepcopy cost (F1 on stack):** ~2.8s/partial cycle — cheaper than re-uploading
  215 blobs; documented in #9.

## Diff deltas per branch (new commits)

- epa-consistency (#1): +4 commits (F1/F2 gate, F3 reorder, F4, F5).
- epa-consistency-tests (#4): +1 commit (F6), rebased onto #1.
- bucket-first-serving (#2): +2 commits (F1 event gate, A7 frontend). Fast-forward.
- blob-gc (#7): rebased only (clean).
- state-snapshot (#9): +2 commits (F1 orig + team-page fresh rows, A5/A6), rebased.
- duckdb-api (#10): +2 commits (A1/A3 fold, A2), rebased (1 conflict resolved).
- db-retirement (#11): +1 commit (A4 + parquet decouple), rebased (2 conflicts resolved).

## Rebase outcomes
Cascade clean except: duckdb-api and db-retirement each had a duplicated
`state-snapshot` commit re-applied over the team-page region (took HEAD/overlay),
and db-retirement's db-less-backend import block (merged nan_safe_eq into the
`src.data.backend` import). All resolved; all branches compile + flake8-F clean.

## PRs updated (`gh pr edit`, chondl/statbotics)
#1, #2, #4, #9, #10, #11 bodies corrected (atomicity language, cost tables, NaN
semantics, bootstrap order, seed-degradation warning). #3, #7 rebased (content
unchanged; #3 force-pushed).
