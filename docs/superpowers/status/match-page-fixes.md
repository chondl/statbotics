# match-page-fixes

Worktree: `.worktrees/match-page-fixes` (branch `match-page-fixes` off `master` @ a2cea55)

## Bugs

- BUG 1 — infinite media-request loop on every `/match/{key}` page.
  Root cause: `frontend/src/pagesContent/match/[match_id]/imageRow.tsx` recreated
  `teams` (a new array via `.concat()`) every render; `useEffect(..., [teams, year])`
  called `getMediaUrls` -> `setMedia` -> re-render -> new `teams` ref -> effect fires
  again -> loop. Fix: wrap `teams` in `useMemo(..., [data])` so its reference is stable.
- BUG 2 — Prev/Next match buttons change the URL but not the page content.
  Root cause: `frontend/src/pages/match/[match_id].tsx` guard `if (!match_id || data) return;`
  blocked refetch once `data` was set, so navigating (match_id changes, component reused)
  never fetched the new match. Fix: `if (!match_id || data?.match?.key == match_id) return;`
  (matches the event page pattern).

## Progress

- [x] Worktree created
- [x] Both bugs reproduced on staging (BEFORE evidence captured)
- [x] Fixes applied (2 commits, one per bug: a60e163, 875433f)
- [x] yarn lint — no warnings or errors
- [x] Local verify (yarn dev vs rig backend + browser)
- [x] Push to fork, open draft PR — https://github.com/chondl/statbotics/pull/5
- [x] Merge into staging (34e6755), redeploy statbotics-web, verify live

## Evidence

- BEFORE (staging live, qm43): 11,747 TBA `/media/` requests in 5s (runaway loop).
  Prev/Next: URL -> qm44, title -> qm44, but displayed match stayed "Qual 43".
- AFTER local (rig backend): 0 media requests in steady state; Prev/Next update
  the displayed match (Qual 43 <-> 44) and shift the arrow links. Event/team pages
  unaffected.
- AFTER live (staging, redeployed statbotics-web rev 00003-42m): 0 media requests
  in 6s; Next -> "Qual 44", links shift to qm43/qm45.

DONE.
