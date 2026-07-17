# Adversarial QA — statbotics staging

Target: https://statbotics.iterativerefinement.com
Backend: https://api-statbotics.iterativerefinement.com
Blob bucket: https://storage.googleapis.com/statbotics-staging-site
Run date: 2026-07-09

Severity: P0 broken / P1 wrong / P2 degraded / P3 polish

Known context (NOT re-reported): match-page repeating request + broken Prev/Next;
2026-only seed (missing historical data is env limit, but bad failure UX is in scope);
upstream `next export` failure.

---

## Findings

## SEVERITY-ORDERED SUMMARY (one line each)

**P0 (broken):** none found beyond the two KNOWN match-page bugs (repeating request + Prev/Next), which are under active repair by parallel agents — observed the tab auto-advancing with `?cachebust=` params; moved on.

**P1 (wrong):** none. All correctness cross-checks (team/teams-list/event/API) agreed exactly.

**P2 (degraded):**
- 8-second blank screen before "not found"/no-data message on any nonexistent or slow entity — `src/pagesContent/shared/notFound.tsx` setTimeout is 8000ms (comment says "one second"). /team/99999, /event/2026zzzzz, /match/2026fake_qm999.
- Noteworthy → "Highest Clean Scores" ranks a 0/null-score placeholder match (`2026txmca sf6-1`, fully-DQed alliance, teams 9990-9999) at #1; combined list sorted ascending. Backend `/noteworthy_matches/2026` returns this order; frontend renders verbatim. Backend + placeholder-seed, not the re-arch.
- /docs/rest ("API" nav link) shows "Error: Server Error" — `src/pages/docs/rest.tsx` hardcodes iframe to PROD `api.statbotics.io/docs`, which is currently HTTP 500 (external outage + pre-existing hardcoded-prod pattern).

**P3 (polish):**
- Duplicate blob fetches on team page (every resource ×2) — `src/api/storage.tsx` `query()` lacks in-flight dedup (pre-existing).
- 7px horizontal page overflow on /teams at 390px mobile (wide table itself scrolls in its own container — fine).
- Latent: `storage.tsx:55` per-minute `?t=` cache-buster on manifest-miss fallback.
- Uncertain/minor: event Simulation table "EPA" column shows 0 for all teams at pre-schedule snapshot (may be intentional delta).

**Not bugs (verified):** Traversal RP -0.34 uniform = backend model data; historical-year (2015) "An error occurred" = graceful env-limit handling; first-match 74-74/50% predictions = legitimate cold start; placeholder/demo teams (9994) render gracefully; nonexistent entities eventually show "not found".

## COVERAGE
Pages/tabs exercised: Home; /teams (Insights, Breakdown tabs; Country=Canada filter; 2015 historical; mobile 390px); /events (Summary + search filter); event 2026casnv (Insights, Elim Matches, Figures, Simulation, SOS, division view) + 2026cmptx Einstein (Insights, Elim Matches, division→2026arc switch); team 254/1678/1690/9994/99999 (Overview, Figures; mobile); match 2026casnv_f1m1 + nonexistent; /matches (Upcoming, Noteworthy); /compare (2-team chart, This Year); header search; /blog + /blog/intro; /docs/rest + /docs/python; deep links (nonexistent team/event/match, historical year, placeholder team).
Coverage gaps (honest): did not click every event tab individually (Alliances/Qual Matches/Bubble Chart verified via rendered DOM dump, not screenshot); only Country filter tested (not State/District); column-header sorting not exercised; match-page depth limited by shared browser with parallel fix-agents; console-error capture unavailable in this tool build (relied on visual + no-crash), so silent console warnings may exist.

---

### Architecture confirmation
- New bucket-first code IS active: `/teams`, `/team/*` fetch ONE `manifest.json` then `v2/{hash}` blob paths. Good.
- Production build confirmed (buildId `iPLZj8yfXUfBDfVVJYHWu`, minified framework) → React StrictMode does NOT double-invoke here.

### P3 — Duplicate blob fetches on team page (pre-existing storage architecture)
- URL: https://statbotics.iterativerefinement.com/team/254
- Network tab: EVERY blob fetched exactly twice — `v2/team_to_events` ×2, `v2/team/254` ×2, and each of 5 event blobs (`2026cancmp`, `2026caclv`, `2026cmptx`, `2026casnv`, `2026cur`) ×2.
- Cause: `src/api/storage.tsx` `query()` has NO in-flight request dedup. Two concurrent callers for the same key both miss IndexedDB (line 112, not yet written) and both fetch. Pre-existing pattern, but wastes bandwidth (each event blob downloaded twice per team-page load).
- Not observed on `/teams` list (single fetch per resource). Team-page components request overlapping event blobs concurrently.

### Note — cache-buster fallback path
- `src/api/storage.tsx:55` `resolveBucketUrl`: when manifest is null AND no versioned/hist entry, appends `?t=${Date.now()/1000/60}` (changes every minute) → defeats HTTP caching. Only hit on manifest-fetch failure; not observed in normal flow but a latent pathology.

### OK — historical year graceful failure (env limitation handled well)
- URL: /teams?year=2015 → tries `hist/1/team_years/2015...` blob then falls back to API `/v3/site/team_years/2015` (each fetched twice), both fail (2026-only seed), shows "An error occurred, please try again later." Graceful. Breakdown tab correctly hidden for non-2026.

### P2 — 8-second blank screen before "not found" (and before any no-data message)
- URLs: /team/99999, /event/2026zzzzz (and any nonexistent/failed entity)
- Repro: load a nonexistent team/event → content area is BLANK for 8 full seconds → then "{Team|Event} not found" appears.
- Cause: `src/pagesContent/shared/notFound.tsx` — `setTimeout(() => setRender(true), 8000)`. The code comment says "wait one second before rendering ... so the not found page doesn't flash", but the value is 8000ms, not 1000ms. Almost certainly a typo/regression (1000 → 8000).
- Impact: 8s of blank white content on any not-found or slow-data page. Looks broken/hung to a user. Affects team page, event page, team-year with no data, etc.
- Confidence: HIGH cause is this file. Whether the 8000 is our regression or pre-existing upstream is unclear — check git blame.

### OK — nonexistent entities eventually handled gracefully
- /team/99999 → "Team not found" (after the 8s delay). Fetches hist/1/team/99999 (+/2026) and API fallback, each doubled (8 total).
- /event/2026zzzzz → "Event not found" (after 8s). 2 data fetches, NOT doubled, no loop.

### Match page (known bugs, active repair) — current state
- /match/2026casnv_f1m1 rendered correctly (Predicted 564-321 RED, WinProb 97%, Actual 516-295 RED; breakdown numbers sane, no NaN).
- While observing, the shared browser tab auto-advanced 2026casnv_f1m1 → 2026mimar_qm43?cachebust=1 → 2026mimar_qm44 (parallel match-fix agents driving the tab, adding ?cachebust= params). Consistent with the known Prev/Next + repeating-request bugs under active repair. Moved on per instructions; did my remaining QA in a separate tab.

### NOT A BUG — Traversal RP = -0.34 uniform is backend data
- API `/v3/team_year/254/2026` returns `traversal_rp: -0.34164` (1690=-0.33955, 4414=-0.34164). Backend model predicts ~-0.34 traversal RP for every team (synthetic 2026 season artifact). Frontend faithfully renders it. Environment/backend data, not a frontend defect.

### P2 — /docs/rest (the "API" nav link) shows "Error: Server Error"
- URL: https://statbotics.iterativerefinement.com/docs/rest
- Actual: unstyled serif "Error: Server Error / The server encountered an error... Please try again in 30 seconds."
- Cause: `src/pages/docs/rest.tsx` hardcodes `<iframe src="https://api.statbotics.io/docs">` (PRODUCTION API, not staging backend). `curl https://api.statbotics.io/docs` → HTTP 500 right now. So this is (a) a pre-existing upstream pattern that hardcodes the prod API URL (staging docs never point at staging backend), plus (b) an EXTERNAL production outage (api.statbotics.io/docs is 500ing). NOT our staging code's fault, but a user on staging clicking "API" hits a broken page.
- Category: external/environment + pre-existing. Confidence HIGH.

### P2 — Noteworthy matches: "Highest Clean Scores" ranks a 0/null-score placeholder match at #1 (backend + seed)
- URL: https://statbotics.iterativerefinement.com/matches → Noteworthy tab
- Actual: "Highest Clean Scores" list shows **1. 0** (match `2026txmca sf6-1`, placeholder teams 9994/9993/9998 vs 9992/9991/9999, score 0-0) ranked ABOVE 2. 964, 3. 956, etc. "Highest Combined Clean Scores" similarly shows 1. 71, 2. 212, 3. 534 (ascending — lowest at top). Same for high_auto/teleop/endgame lists.
- Root cause: BACKEND. `GET /v3/site/noteworthy_matches/2026` returns `2026txmca_sf6m1` at the TOP of high_score/high_auto/high_teleop/high_endgame (and `2026sccha_f1m1` atop combined_score). That match is status "Completed" but has the ENTIRE red alliance DQed (`dq_team_keys:[9994,9993,9998]`) with a null/0 result — a placeholder synthetic event (teams 9990-9999). The backend's noteworthy sort places null/DQ-result matches first instead of filtering/sorting them last.
- Category: backend noteworthy-query bug + placeholder-seed data. Frontend (`src/api/matches.tsx` getNoteworthyMatches → `src/pagesContent/matches/noteworthy.tsx`) renders the backend order verbatim, so it faithfully shows "0" at #1. Frontend COULD defensively drop null-score / fully-DQed matches, but the primary defect is backend/seed. NOT the bucket-first re-arch.
- Confidence: HIGH (verified against raw API).

### OK — /matches Upcoming, event Figures/Simulation/SOS tabs render
- /matches Upcoming: "No upcoming matches" (correct, season complete in seed). Filters present.
- Event Figures: Highcharts "Top 16 Teams by EPA" stacked bar + "Team EPA vs Rank" scatter render (125 svgs, highcharts present).
- Event Simulation: runs 1000 live sims, predicted-rank table with mean/percentile ranks + mean RPs renders; no hang/loop. MINOR/uncertain: the sim table "EPA" column shows 0 for every team at the "Before Schedule Release" snapshot (SOS "Before Event" correctly shows 23.7 baseline / 218.8 for a team with a prior event). Possibly intentional (delta) — low confidence, low severity.
- Event SOS: renders RP/Rank/EPA/Composite scores with Before/After Event toggle; values plausible.

### OK — Blog, docs/python, compare, header search all work
- /blog: 5 posts render. /blog/intro: heading + 5 images all loaded (naturalWidth>0), 5.4k chars text.
- /docs/python: iframe → statbotics.readthedocs.io (external, loads).
- /compare: added 254 + 1690, Total-EPA-vs-match chart renders both curves; peaks (254 ~360, 1690 ~410) match API max values 359.26 / 410.16. "This Year"/"All Time" tabs, metric dropdown present.
- Header search: typing "254" returns 254/2543/2549/10254/11254 with names; clicking result navigates to /team/254.

### OK — CORRECTNESS cross-check PASSES (team page vs teams list vs event insights vs API)
- Team 254: API total_points=328.0, auto=78.48, teleop=169.83, endgame=79.69, ranks total/country/state = 3/2/2, record 65-6-0, unitless 2257. All match /team/254 page, /teams list, and /event/2026casnv insights (event EPA 291.6 also matches team page event section). No inconsistencies found across views.



