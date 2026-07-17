# PR consolidation pass — status log

Consolidating the Track 1 UI bug fixes on `chondl/statbotics` (fork). Restructures
PRs #5/#6/#8 and relocates one change onto the Track 2 stack (#2). No code changes
beyond the described relocations. `git push` fork only, never origin. `gh` on
`chondl/statbotics` only.

## Plan (user's intent)

1. PR #5 (`match-page-fixes`) becomes the single UI-fixes PR. Add from #6: storage.tsx
   in-flight dedup + noteworthy `.nullslast()`. Add from #8: both fixes (worker.ts
   variance floor + simulation.tsx EPA field). NOT the notFound timeout. Retitle +
   rewrite body.
2. notFound.tsx 8000→1500ms moves onto #2 (`bucket-first-serving`); cascade-rebase the
   stack + tests branch; force-push all.
3. Close #6 and #8 (superseded), keep branches.
4. Conflict-doc migration: storage.tsx conflict moves #6↔#2 → #5↔#2. Verify by merge
   sim. Update #5/#2/#1 bodies.
5. Update playbook `migration-overview.md` + addendum in `pr-organization.md`.
6. Verify: lint, byte-equivalence, flake8, stack fast-forwards, notFound in #2,
   db-retirement compiles.

## Source commits

- qa-fixes (#6): `1a6b476` notFound 8s→1.5s | `f68ed73` storage.tsx dedup | `bdee5e7` noteworthy nullslast
- sos-sim-fix (#8): `6e93c47` worker.ts variance floor | `75151b8` simulation.tsx EPA field
- All touch disjoint files; cherry-picks apply independently.

## Execution log

### 1. PR #5 rebuilt (`match-page-fixes`)
Cherry-picked onto the existing 2 match-page commits, in order:
`eb71894` SOS variance floor · `e690125` simulation EPA field · `4b9275c` noteworthy
nullslast · `c7c60bb` storage.tsx dedup. All applied clean (disjoint files). New tip
`c7c60bb`; old tip `875433f`. Force-pushed to fork.
- Byte-equivalence: storage.tsx + noteworthy vs `fork/qa-fixes` — identical; worker.ts +
  simulation.tsx vs `fork/sos-sim-fix` — identical. notFound.tsx == master (correctly NOT
  brought). `yarn lint` clean; `flake8` clean on noteworthy_matches.py.
- Retitled `[fix] UI: match pages, SOS/simulation, noteworthy ordering, duplicate fetches`;
  body rewritten (6 per-fix sections + consolidated verification + #5↔#2 conflict note).

### 2. notFound → #2, stack cascade-rebased
`1a6b476` cherry-picked onto `bucket-first-serving`; notFound.tsx byte-identical to
`fork/qa-fixes`. New #2 tip `740397e` (old `3d747b9`). Cascade-rebased, all clean, all
fast-forward from parent:
- blob-gc `0338012`→`e0faa64` · state-snapshot `587199e`→`fe6fcc4` ·
  duckdb-api `9861cc6`→`19bcf4e` · db-retirement `7ad45c5`→`35ebd9b` ·
  bucket-first-serving-tests `6fa5b86`→`8fa8791`.
- (Local tests branch was stale `3aaa6d4`; reset to fork's `6fa5b86` first — fork's copy
  was already on #2's full tip, so the old off-tip note is resolved.)
- db-retirement backend `compileall` exit 0; backend byte-identical to `fork/db-retirement`
  (rebase touched only the frontend base). All six branches force-pushed.
- PR #2 body: banner conflict list `(#1, #6)`→`(#1, #5)`; added not-found rationale
  paragraph; storage.tsx conflict `#6`→`#5`.

### 3. Closed #6, #8
Both CLOSED on chondl/statbotics with superseded comments; branches `qa-fixes` /
`sos-sim-fix` retained on fork.

### 4. Merge-sim conflict migration (scratch, never pushed)
Scratch branch off master + #5 + #12 + #1 (all clean) then + #2. Conflicts:
- `frontend/src/api/storage.tsx` — **#5↔#2** (HEAD carries #5's `inFlight`/`fetchAndStore`
  dedup, formerly #6's). Resolution take-#2's-rewrite verified: resolved file == #2's.
- `backend/src/google/storage.py` — **#1↔#2**, unchanged. Take-#2 resolution: #1's
  non-storage fixes survive (CUTOFF=200, read_tba deferral, utils.py nan_safe_eq, main.py
  ordering).
Scratch worktrees + `sim-consolidation` branch deleted. Documented in #5 body, #2 body,
and the playbook.

### 5. Playbook
`migration-overview.md`: Part 1 narrative → three PRs (#5 consolidated UI, #12, #1) +
test note; Track 1 table → `#5 → #12 → #1` with #5's expanded one-liner; #6/#8 rows
removed; conflict section storage.tsx `#6`→`#5`. No stale #6/#8/qa-fixes/sos-sim refs
remain. `pr-organization.md`: §7 addendum added. PR #1 body checked — no #6/#8 refs.

### 6. Verification — all green
lint clean; four relocated files byte-equivalent to source branches; flake8 clean;
stack all fast-forwards; #2 contains notFound 8000→1500; db-retirement compiles. Staging
untouched.
</content>
