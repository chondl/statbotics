# PR organization pass — status log

Final pre-submission organization of the 12 draft PRs on `chondl/statbotics`.
Scope: metadata audit + merge simulation + progression check + playbook. No code
changes, no branch pushes, `gh pr edit` on `chondl/statbotics` only.

## 1. Inventory + base-branch audit

master = `a2cea5553e35693d423400f419bd770cb2143408`. Every branch roots at master.

| PR | head | base (actual) | base (intended) | ok |
|----|------|---------------|-----------------|----|
| #5 | match-page-fixes | master | master | yes |
| #12 | quality-fixes | master | master | yes |
| #8 | sos-sim-fix | master | master | yes |
| #6 | qa-fixes | master | master | yes |
| #1 | epa-consistency | master | master | yes |
| #4 | epa-consistency-tests | epa-consistency | #1 | yes (on #1 tip) |
| #2 | bucket-first-serving | master | master | yes |
| #3 | bucket-first-serving-tests | bucket-first-serving | #2 | base ok; **branched off an older #2 commit** (see note) |
| #7 | blob-gc | bucket-first-serving | #2 | yes |
| #9 | state-snapshot | blob-gc | #7 | yes |
| #10 | duckdb-api | state-snapshot | #9 | yes |
| #11 | db-retirement | duckdb-api | #10 | yes |

Stack ancestry verified linear: #2 ⊂ #7 ⊂ #9 ⊂ #10 ⊂ #11.

**#3 off-tip note:** `bucket-first-serving-tests` merge-base with `bucket-first-serving`
is `4de126d`, not #2's tip `3d747b9`. #3 is missing #2's last commit ("Serve match
pages from the edge-cached event blob; unblock team event wave"). GitHub's three-dot
diff still shows only the test commit, and #3 merges cleanly onto #2, so it is not a
blocker — but the tests were written against #2 minus its final commit. Cannot rebase
(no branch pushes allowed); flagged for the maintainer / a follow-up.

## 2. postgres-compat isolation

postgres-compat commits `c99681d`, `f8d8026`, `a48c23e` are **not ancestors** of any
upstream-bound branch. Content check for its signatures (`DATABASE_URL` override,
`run_transaction` dialect dispatch, ORM `BigInteger` on timestamp columns) across all
stack + bug-fix tips: **no leak**. (#10's Parquet int64 discussion is a Parquet-schema
choice, unrelated to postgres-compat's ORM change.)

## 3. Merge simulations (local scratch worktree, nothing pushed)

### (a) Track 1 in recommended order onto fresh master: #5 → #12 → #8 → #6 → #1
All five merge **clean**. No conflicts between the bug-fix PRs. They touch mostly
disjoint files; the one shared backend file (`read_tba.py`, touched by #12 P2 and #1
item 3) merges clean (different regions). **Order is not load-bearing within Track 1.**

### (b) Track 2 stacked on top of Track 1 — cross-track conflicts
Merging #2 onto a tree that already has Track 1 produces **two** conflicts:

1. **`backend/src/google/storage.py` — #1 ↔ #2.** Both rewrite the publish path and both
   carry an event-content gate. **Resolution: take #2's version** (manifest / content-
   addressed `_publish` / `UploadPlan` + its own `nan_safe_eq` event-content gate). This
   subsumes #1's blob gate. **#1's other fixes survive untouched** because they live in
   other files: `data/utils.py` (`nan_safe_eq` DB write gate), `tba/read_tba.py`
   (`defer_missing_breakdown`, 24h grace), `db/write/template.py` (`CUTOFF = 200`),
   `data/main.py` (publish-before-DB ordering). Verified after resolution: all four
   present; `write_objs` signature is identical in #1 and #2 `(objs, orig_objs=None)`, so
   #1's reordered `write_objs_storage(...)` call binds to #2's manifest writer cleanly.
   **The documented resolution matches what the merge actually produces.**

2. **`frontend/src/api/storage.tsx` — #6 ↔ #2.** Both add in-flight fetch dedup. #6 adds
   query-level dedup keyed by `storageKey` (`inFlight` + `fetchAndStore`); #2 rewrites the
   file for bucket-first serving with blob-level dedup keyed by `logicalPath`
   (`bucketInFlight` + `fetchBucketData`) plus stale-if-error. **Resolution: take #2's
   rewrite** — its `bucketInFlight` dedups the double *blob* fetch that was #6's actual
   symptom ("each event/team blob downloaded twice"), so #6's fix is functionally
   subsumed. #6's query-level (API-path + IndexedDB) dedup is dropped by taking #2 whole;
   a maintainer who wants it back re-wraps #2's `query()` in the `inFlight[storageKey]`
   pattern. Not a blocker.

No other Track-1 PR conflicts with #2: #5, #12, #8 each + #2 merge **clean**.

### (c) Track 2 alone on bare master: #2 → #7 → #9 → #10 → #11
Linear descendant chain of master → every merge is a fast-forward, **zero conflicts**.
The architecture stack can be taken first, without any bug fix.

## 4. Progression check (reading bodies in recommended order)

- No forward references to unmerged work that read as dependencies: #2 mentions the GC as
  a "stacked follow-up" (accurate); #7/#9/#10/#11 reference only earlier stack PRs.
- No stale numeric claims across the recommended order. #9's "honest-diff gate compares
  `str(obj)`" is correct in the Track-2-only context (utils.py is unmodified there); #1's
  switch to attrs `__eq__` only applies if Track 1 also lands, and is compatible.
- No fork-internal references in any body (no agent names, status files, superpowers
  paths, secrets). Staging URLs appear only as evidence, which is allowed.

## 5. Edits made (gh pr edit on chondl/statbotics)

Adopted title convention: `[fix]` (bug fixes), `[fix·tests]`/`[blob-store tests]`,
`[blob-store N/5]` (stack). Prepended a one-block metadata banner (track · base ·
dependencies · recommended slot · companion tests) to every body. Added the #1↔#2
storage.py conflict + resolution to **#1** (was missing) and tightened it in **#2**;
added the #6↔#2 storage.tsx conflict note to **#6** and **#2**; added the off-tip note to
**#3**. Per-PR detail in the final report.

## 6. Blockers

None at code level. Two organizational notes carried to the playbook: #3 sits off #2's
tip (cosmetic), and the #6↔#2 storage.tsx interaction drops #6's query-level dedup unless
re-applied.

## 7. Addendum — consolidation pass (supersedes parts of §1, §3, §5)

A later pass consolidated the Track 1 UI fixes (full log: `pr-consolidation.md`):

- **#5 (`match-page-fixes`) is now the single UI-fixes PR.** It absorbed, as clean
  cherry-picks, the `storage.tsx` in-flight dedup and the `noteworthy_matches.py`
  `.nullslast()` fix from #6, plus both fixes (worker.ts variance floor, simulation.tsx
  EPA field) from #8. Retitled `[fix] UI: match pages, SOS/simulation, noteworthy
  ordering, duplicate fetches`.
- **#6 and #8 are CLOSED** (superseded; branches kept). Track 1 is now three PRs:
  `#5 → #12 → #1` (+ #4 tests).
- **The not-found 8s→1.5s change moved onto #2** (`bucket-first-serving`) as a commit —
  rationale: the long debounce papered over slow API-backed loads; bucket-first serving
  makes the short timeout safe. The whole stack (`#7/#9/#10/#11`) and the tests branch
  (#3) were cascade-rebased onto the new #2 tip and force-pushed; all still fast-forward
  from their parents. The §1 "#3 off-tip" note is resolved — the fork's #3 was already on
  #2's full tip and the rebase carried it forward cleanly.
- **The §3(b) storage.tsx cross-track conflict migrates #6↔#2 → #5↔#2** (same take-#2's-
  rewrite resolution; re-verified by merge simulation on scratch branches). The
  storage.py #1↔#2 conflict is unchanged. Conflict docs in #5/#2 bodies and the playbook
  updated accordingly.
