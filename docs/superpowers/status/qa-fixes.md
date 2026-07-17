# QA fixes — progress

Source: `docs/superpowers/status/qa-adversarial.md`. Four issues from the
adversarial staging QA pass.

Worktree: `.worktrees/qa-fixes` (branch `qa-fixes`, off `master`).
Draft PR (fork only): https://github.com/chondl/statbotics/pull/6

## Fixes

| # | Area | File | Status |
|---|------|------|--------|
| 1 | Frontend | `frontend/src/pagesContent/shared/notFound.tsx` | DONE — 8000ms → 1500ms |
| 2 | Frontend | `frontend/src/api/storage.tsx` | DONE — in-flight dedup by storageKey |
| 3 | Backend | `backend/src/db/functions/noteworthy_matches.py` | DONE — `.nullslast()` on all order_by |
| 4 | Staging only | `frontend/src/pages/docs/rest.tsx` | DONE (staging branch only) — iframe derives from BACKEND_URL |

## Root causes (verified)

1. **Not-found 8s blank.** `setTimeout(..., 8000)`. History: 1000 (2023-01) →
   5000 → 8000 (2024-03), deliberate upstream growth, comment still says "one
   second". Reduced to 1500ms — enough to suppress the flash, no longer looks
   hung.
2. **Duplicate fetches.** `query()` had no in-flight dedup; concurrent callers
   for the same storageKey all miss IndexedDB and each fetch. Added a
   module-level in-flight promise map keyed by storageKey (fetch body extracted
   to `fetchAndStore`).
3. **Noteworthy null match at #1.** `order_by(desc(...))` did not pin null
   placement. CockroachDB orders NULLs FIRST under DESC; a match with no clean
   result on either alliance (`greatest(no_foul,no_foul)=NULL`, and `sum=NULL`
   for combined) sorted to #1. `.nullslast()` on every order_by. The rig's
   CockroachDB happens to default NULLS LAST, so the bug only manifests on the
   staging DB; verified by forcing NULLS FIRST on the rig (placeholder
   `2026txmca_sf6m1`/`_sf9m1` ranked #1-2 before, gone after; real 964-pt match
   `2026dal_f1m1` #1 across all six lists after).
4. **/docs/rest.** Hardcoded `https://api.statbotics.io/docs` (prod, 500ing).
   Staging-only commit derives the iframe from BACKEND_URL →
   `https://api-statbotics.iterativerefinement.com/docs`.

## Verification

- Backend fix 3: rig before/after (script, DB row temp-nulled + restored) — PASS.
- `yarn lint` clean (qa-fixes + staging). Backend `flake8`/`black`/`isort` clean.
- Live staging (post-deploy, browser):
  - Fix 1: "not found" text ~1598ms after client-side route push to
    /team/99996 (was 8000ms+); deployed JS chunk contains `1500`, no `8000`.
  - Fix 2: /team/118 fresh load — 14 requests / 13 unique resources; all
    `query()`-path blobs single-fetched. Residual: `team_to_events` x2 via
    Track 2's `fetchBucketData()` (bypasses `query()`; staging-only code path,
    absent on master — out of scope for the upstream fix).
  - Fix 3: staging API + regenerated bucket blob both rank `2026dal_f1m1`
    (964) #1; UI shows Highest Clean Scores 964/956/903... and Highest
    Combined 1561/1520... descending; placeholder gone from all six lists.
  - Fix 4: /docs/rest iframe src =
    https://api-statbotics.iterativerefinement.com/docs, Swagger UI renders.

## Staging — DONE

- `qa-fixes` merged into `staging` (merge commit `ee066d0`); storage.tsx conflict
  resolved by adding the dedup onto staging's manifest-aware version (stale
  fallback kept in `query()` where `minLength` is in scope).
- Staging-only `/docs/rest` commit `3774e00`. `staging` pushed to `fork`.
- Redeployed both Cloud Run services: `statbotics-api-00005-8pd`,
  `statbotics-web-00004-pkk`. Ran `/v3/data/update_curr_year` (66s, success) to
  regenerate the noteworthy blob (manifest cycle 2026-07-10T06:54:47Z, blob
  `v2/noteworthy_matches/2026.3f86ac0bfc04`).

## Constraints honored

- origin never pushed; no PRs/issues/comments on avgupta456/statbotics; nothing
  external. Draft PR + staging push on `fork` only. No tests on the fix branch.
