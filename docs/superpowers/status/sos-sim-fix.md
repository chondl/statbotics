# SOS + Simulation tab fixes — statbotics

STATUS: DONE (deployed to staging + verified live 2026-07-10)

Branch `sos-sim-fix` (off master `a2cea55`), pushed to fork only. Draft PR
**chondl/statbotics#8**. Two commits, 3 changed lines total across 2 files. Both
bugs are **pre-existing upstream**: `worker.ts`, `sos.tsx`, and `simulation.tsx`
are byte-identical between master and every workstream branch (verified with
`git diff` across bucket-first-serving, qa-fixes, match-page-fixes,
epa-consistency, blob-gc, staging).

## Bug 1 — Strength of Schedule tab blank (or NaN)

- **File/line:** `frontend/src/pagesContent/event/[event_id]/worker.ts:573`
- **Root cause:** the "Before Event" pass computes `epaSd` over every team's
  pre-event `epa.stats.start`. All 2026 events serve an identical cold-start
  value (23.74) for every team, so `epaSd` is `0` → `Gaussian(0, 0)` throws
  `Variance must be > 0` (gaussian@1.3.0 guard). The worker's message handler
  invokes `strengthOfSchedule(...)` without await/catch, so the throw is a
  silent unhandled rejection inside the worker — no message posted, score
  columns stay blank forever. Float variant: `Math.sqrt` of a tiny negative rounding
  residue makes `epaSd = NaN` → `Gaussian(0, NaN)` does NOT throw → EPA/
  Composite render `NaN` (observed on 2026mimar; the throw variant on
  2026sccha).
- **Why Simulation still worked:** it never runs the SOS metric loop / Gaussian.
- **Fix:** `Gaussian(0, (epaSd * epaSd * 5) / N || 1e-9)` — `||` treats both `0`
  and `NaN` as falsy; degenerate case yields the semantically correct neutral
  0.5 EPA percentile; any real positive variance passes through unchanged
  (verified numerically: control event 2026wasno percentile 0.387 before ==
  after).

## Bug 2 — Simulation tab EPA column shows 0 for every team

- **File/line:** `frontend/src/pagesContent/event/[event_id]/simulation.tsx:140,161`
- **Root cause:** reads `teamEvent.epa.total_points.mean`, matching the stale
  `APITeamEvent` type (`frontend/src/types/api.tsx:208`) but not the runtime
  shape — the backend serves `epa.total_points` as a plain number, so `.mean`
  is undefined → `?? 0`.
- **Fix:** read `epa.breakdown.total_points` (the field the worker and SOS tab
  already use).

## Ruled out during diagnosis

- The cheesy-arena schedule CSV fetch (`27_12.csv` → 200, valid CSV).
- Workstream branch regressions (files identical to master everywhere).
- CORS/CSP/worker-chunk deployment causes (worker runs; preSim path posts fine).

## Verification (live staging, real browser)

Before fix — 2026sccha `#sos`: RP/Rank/EPA/Composite all empty; 2026mimar
`#sos`: EPA/Composite `NaN`; Simulation EPA column 0 for all teams.

After fix (rev `statbotics-web-00007-8p2`):
- 2026sccha SOS: all 4 score columns numeric (e.g. 0.51/0.64/0.50/0.55), 0
  empty/NaN cells over all rows; Reload control re-runs the worker (10/10 rows
  changed).
- 2026mimar SOS: numeric, 0 bad cells; "After Event" toggle switches EPA to
  post-event values (131.8/96.6) with non-degenerate EPA Scores (0.29/0.18).
- Simulation (both events): EPA column real per-team values (175.3/73.6/… and
  131.8/96.6/…); Reload varies Mean Rank/RPs stochastically (9/9 rows changed).
- Blob fetches on `blobs-statbotics.iterativerefinement.com` (cf-blob-proxy
  cutover carried in the final image); zero window.onerror / unhandledrejection
  events.
- 2026wasno (natural control, epaSd > 0): SOS rendered before AND after —
  regression-free.

## Staging landing

Merged `sos-sim-fix` into `staging` (merge `765cec0`), pushed to fork. Two
web-image builds: `a739ccf7` (fix only — transiently reverted cf-blob-proxy's
BUCKET_URL; noted in COORDINATION.md) then `a4ca7ac2` (fix +
`BUCKET_URL=https://blobs-statbotics.iterativerefinement.com`), deployed as
`statbotics-web-00007-8p2` (100% traffic). Backend untouched. Smoke 5/9 — the 4
FAILs are DB-backed reads 500ing during historical-data's announced
`reset_all_years` backfill (blob reads + liveness all PASS); unrelated to this
change.

## Deferred / noted, not fixed

- Stale `APITeamEvent.epa.total_points: { mean, sd }` type (api.tsx:208) —
  runtime is a plain number. Fixing the type is a wider refactor; the display
  bug is fixed at the read sites.
- The worker's message handler still doesn't catch rejections; any future
  worker throw will again fail silently. A `.catch(postMessage error)` wrapper
  would surface failures, left out to keep the diff minimal.
