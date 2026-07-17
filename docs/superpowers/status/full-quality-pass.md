# Full Quality + Performance Pass — Staging

Auditor: full-quality-pass agent. Date: 2026-07-10. READ-ONLY audit.

Targets:
- Frontend: https://statbotics.iterativerefinement.com
- API: api-statbotics.iterativerefinement.com (/v3, DuckDB-over-Parquet)
- Blobs: blobs-statbotics.iterativerefinement.com (CDN)

Severity: P0 (broken/data-loss) · P1 (major, user-visible wrong) · P2 (notable) · P3 (minor/cosmetic).
Tags: NEW vs KNOWN.

---

## Code-level fetch architecture notes (from .worktrees/staging/frontend/src/api/, read-only)

- `storage.tsx` `getManifest()`: single shared promise cached 60s (`MANIFEST_TTL_MS`). Every blob
  fetch `await getManifest()` first → manifest.json gates the FIRST blob on a cold load
  (one RTT serialization, then blobs fan out). `resolveBucketUrl` uses versioned map, else
  `hist/{hist_epoch}/{path}`, else legacy `?t=` path.
- `fetchAndStore` (storage.tsx:126): bucket-first when `checkBucket` true, else straight to
  `BACKEND_URL` (Cloud Run). In-flight dedup via `inFlight` map. IndexedDB TTL cache.
- **`getMatch` (match.tsx:8) passes `checkBucket=false`** → match detail data ALWAYS hits the
  backend API (Cloud Run), never the blob CDN. Cold-start / serial risk candidate.
- **`getTeamYears` (team.tsx:83, team/years) `checkBucket=false`** → backend.
- `getTeamYear` current-year (team.tsx:15): reconstructs from blobs in TWO serial waves —
  wave 1 `Promise.all([team_to_events, getTeam, getYearTeamYears])`, then wave 2
  `Promise.all(team_to_events[team].map(getEvent))`. Events wave gated on team_to_events.
  Non-current-year: single `/team/{team}/{year}` blob.
- `getEvent` / `getYearEvents` / `getYearTeamYears`: single blob each (current year), backend for
  historical on some paths.
- noteworthy_matches non-current-year `checkBucket=false` → backend.

## Findings (chronological)

### First-hand verified (auditor, own Chrome)
- **OK — team /team/254 (2026)**: overview correct (record 65-6-0; EPA Auto 78.4/Teleop 169.7/
  Endgame 79.7/Total 327.8; ranks 3/2/2; video icons; score+pred+winpred columns; green highlight).
  No NaN. Figures tab "EPA Over Time" renders a clean per-season curve (x-axis=Match 0-70, rises
  ~113→plateau ~330, peak ~360). Populated, tooltips-capable.
- **CLARIFICATION (not a bug) — "multi-year 2002-2026 graph"**: the team Figures tab is a PER-SEASON
  EPA-over-match chart, not a single 2002-2026 career line. The multi-year dimension is the year
  selector (now populated with all historical years) + historical team-year pages. Note the 2026
  curve now STARTS at ~113 = the history-seeded `epa_start` 113.95 (per historical-data.md), so
  historical seeding is correctly reflected. (Agent A investigating whether any career-span chart
  exists/should exist.)
- **OK — match /match/2026cmptx_f1m1 (Einstein Finals 1)**: summary correct incl. an UPSET rendered
  right (projected RED 849-738 @84%, actual BLUE 518-456). Full breakdown table (Auto/Teleop/Endgame/
  Fouls/RP1-3/Total, predicted vs actual, percentile key). No NaN. **Only 4 network requests total —
  NO media-request loop** → regression-confirms match-page-fixes BUG1 (media loop) is fixed.
- **OK — historical team /team/254/2015**: full Recycle Rush schedule renders w/ correct 2015 scores;
  Total EPA 115.8, worldwide rank #1 of 2873. Record shows "4-0-0" — **KNOWN/expected**: 2015 quals
  all have winner=None (Recycle Rush no head-to-head), only elim wins count; matches prod
  (historical-data.md). Spot-check 254/2015 norm_epa 2004 rank#1 CONFIRMED.
- **OK — event /event/2026casnv** and **/teams** and **/team/254/2015** all render, no NaN in body.
- **REGRESSION FIXED (was P3 in qa-adversarial.md) — duplicate blob fetches ×2 on team page**: current
  storage.tsx has in-flight dedup (`inFlight` + `bucketInFlight` maps). My cold /team/254 waterfall
  showed each blob fetched exactly ONCE (team_to_events, team/254, 5 event blobs — no ×2). Fixed.

### KNOWN (from status docs — not re-reported as new)
- manifest.json `cf-cache-status: DYNAMIC` is a DELIBERATE choice (cf-blob-proxy.md): the zone's 4h
  Browser-Cache-TTL floor would stretch the 60s manifest TTL, so the worker sets
  `cacheEverything:false` for /manifest.json only. My perf note quantifies its critical-path latency
  cost (a new *angle*, not a new bug); a short edge-TTL alternative could keep the 60s freshness while
  removing the origin RTT.
- 2015 team_years summary = 507 of 2873 (filtered to count>0 elim-competing teams) — legitimate.
- Accepted P3s: 7px overflow @390px, legacy `?t=` manifest-miss path, tie-order nondeterminism.
- Prior-QA P2s still expected (pre-existing/backend/external): 8s blank before "not found"
  (notFound.tsx setTimeout 8000ms), noteworthy "Highest Clean Scores" ranks a 0/DQ placeholder match
  #1 (backend sort), /docs/rest iframe → prod api.statbotics.io 500.
- Match-page regressions (media loop, Prev/Next guard) fixed per match-page-fixes.md (DONE).
- SOS/Sim variance-floor + EPA-column fixes done + verified on 2026sccha/2026mimar/2026wasno
  (sos-sim-fix.md).

### Subagent breadth results (folded in below as they report)

#### Agent: event tabs + Simulation/SOS interactive (completed)
- **SIMULATION 2026casnv — PASS (regression check)**: table fully populated (Pred Rank, EPA, Mean/5%/
  Median/95% Rank, Mean RPs), zero NaN/blank. Reload clicked 2x: outputs VARY run-to-run (e.g. Mean
  Rank 7.57→7.12, Mean RPs 34.74→34.98; 10.67→11.32) and stay in sane ranges (ranks 1-30, RPs 30-48).
  Variance-floor fix healthy.
- **SOS 2026casnv — PASS**: RP/Rank/EPA/Composite scores all numeric ~0-1, no NaN; Before/After
  Event toggle present.
- **2026casnv all tabs — PASS**: Insights, Breakdown, Bubble Chart, Qual Matches, Alliances, Elim
  Matches, Figures each screenshot-verified; numeric tables, percentile badges, charts render; no
  NaN/undefined/blank columns.
- **Historical events (2015casj regional, 2015necmp district champs)**: Insights + Bubble Chart OK.
  Simulation/SOS/Breakdown tabs NOT RENDERED for historical years — graceful omission (projection
  tabs meaningless for completed pre-2026 events); no broken/blank tab. NOTE: coverage of the
  remaining historical tabs (Qual/Alliances/Elim/Figures) was cut short by browser contention (below).
- **P3 NEW — 2015 Insights "Ranking Score" column shows 0.00 for every team** (2015casj, 2015necmp).
  VERIFIED against hist/1/event/2015casj blob: qual `rank` values are real (7/57/26...) but
  `rps`/`rps_per_match`=0 for all — 2015 had no ranking-point system (ranked by Qual Average) and
  quals have winner=None, so the data is model-consistent, same family as the KNOWN 2015 quirk.
  Cosmetic recommendation only: hide the RS column (or show the year-appropriate metric) for 2015.
- **FALSE POSITIVE (discounted) — reported "P0 spontaneous auto-navigation every 5-15s"**: artifact
  of the audit itself, not the site. Three audit subagents + auditor shared ONE Chrome instance; the
  "spontaneous" destinations (/match/2026casnv_qm1, /team/254, /teams) are exactly the pages the
  other agents were driving at that moment via CDP; the captured Router.changeState stack shows the
  Next router executing those externally-driven navigations. Auditor independently observed the same
  tab-hijack. Single-controller sessions earlier in this audit (long dwells on /team/254 and match
  pages) produced zero spontaneous navigations and bounded request counts.
- **NOT A BUG — /event/2015newton, /event/2015curie 404**: wrong keys guessed; 2015 champs division
  keys are the short TBA forms. Verified via blob: hist/1/event/2015new → 200, 2015cur → 200.

#### Agent: match nav + historical year switcher (completed; key items re-verified by auditor)
- **P0 NEW — /teams?year=2021 crashes**: "Application error: a client-side exception has occurred"
  (blank page). Reproduced 2/2 by agent AND independently by auditor (3/3 total). /events?year=2021
  degrades gracefully ("An error occurred, please try again later.") — the teams page lacks the
  equivalent guard for the no-season year. Explicit requirement was graceful 2021 handling.
  Suspect: teams-page render path missing null/empty guard when team_years/2021 has no data.
- **P1 NEW — historical pre-component-era match pages show literal "NaN"**: /match/2015casj_qm1
  Match Breakdown renders NaN badges in the Predicted column for Auto/Teleop/Endgame/Fouls on BOTH
  alliances (auditor-verified via screenshot). Total predictions are fine (28-89, 93%) — 2015 EPA
  has no per-component breakdown, and the alliance Predicted cells sum missing fields → NaN instead
  of the "N/A" used elsewhere in the same table. 2019 match pages are clean (component EPA exists),
  so the affected surface is likely all 2002-2015 match pages. Fix direction: render N/A / hide
  component rows when the year model lacks breakdowns.
  - Sub-note (KNOWN family): "Actual Winner:" label is blank on the 2015 qual match despite score
    40-176 — consistent with the 2015 winner=None model quirk, but a graceful display ("N/A") would
    be better than an empty label.
- **Prev/Next regression — PASS (quals)**: 2026casnv qm1→qm2→qm1 navigates correctly, content
  updates, resource count grows bounded (70→82→90) — NO request loop. Same on 2015/2019 quals
  (Next present; no Prev on first match, correct).
- **P3 NEW (confirm intent) — elim match pages have NO Prev/Next nav at all** (2026casnv_f1m1,
  _f1m2 checked): quals have adjacent-match arrows, elims none. Possibly intentional (bracket not
  linear), worth confirming.
- **Year switcher sweep — PASS except 2021**: 2002 (sparse, 19 events, renders fine), 2008
  (Simbotics #1), 2015 (**254 rank 1 norm EPA 2004 — spot-check CONFIRMED**; 118 events), 2019
  (full column set; 254 rank 5, sane top teams), 2025 (full breakdown columns). Teams + events
  lists populate for all.
- **/matches — PASS**: Upcoming gracefully empty ("No upcoming matches"); Noteworthy tables populate
  with real data, no NaN/undefined. (The prior-QA P2 placeholder-match-at-#1 was not re-observed in
  the agent's view; not re-verified in depth.)
- Match pages detail: 2026casnv_qm1 (future-style prediction 87-205 85%, video gracefully absent),
  2026casnv_f1m1 (575-332, 97%, actual 516-295, clean), 2019casj_qm1 (41-50, 66%, actual 46-44,
  video iframe present, clean).

#### Agent: team pages + diagrams sweep (completed; headline re-verified by auditor)
- Per-team: 254 OK; 1678 OK; 2521 (mid-pack) OK (chart Match 0-40, EPA ~58-98, sane); 11247 (rookie)
  OK — short 2026-only history graceful (Match 0-24, EPA ~6-31); team 9 (genuinely inactive since
  2008) OK — page defaults to **2008** (last active year), record 5-4-1, period-correct rank
  denominators (out of 1498), Figures ends at match 10, Total-only EPA (2008 pre-component model —
  correct). Teams 71/45/191 turned out to still be active in 2026 (not usable as inactive cases).
- No NaN/undefined/Infinity in any captured team page; no broken/empty charts.
- Same shared-browser "auto-navigation P0" reported → same FALSE POSITIVE (see above). Auditor's
  definitive test after all agents finished: 25s untouched dwell on /team/254 → URL never changed,
  resource-count delta 0. No auto-navigation, no request loop. CONCLUSIVE.
- **HEADLINE ANSWERED (auditor, first-hand) — multi-year EPA history EXISTS and spans 2002-2026**:
  the sweep initially said "No" because the career view is not on the Figures tab — it is the
  **"Summary" option in the team-page year dropdown**. Selecting Summary on /team/254 renders
  "Normalized EPA over Time" with x-axis 2002→2026, fully populated line (2002 ~1690 → 2017 peak
  ~2060 → 2026 ~1944; 2015 point ≈2004 matching the spot-check), Baseline series at 1500, plus a
  "Years Summary" table with per-year Norm EPA + world/country/district/state ranks & percentiles
  for every active year (2018 rank #1 = 254's championship year, correct). 2021 correctly absent
  from both the dropdown and the table; the line interpolates smoothly across the 2021 gap. No NaN.
  Summary data loads via `/team/{num}/years` (backend; checkBucket=false in team.tsx:83).
- Minor coverage note: chart hover-tooltips on the Summary chart not conclusively exercised
  (hover captured but tooltip content not extracted); metric dropdown on team Figures verified
  present (react-select), options not individually cycled.

#### Auditor follow-ups on historical events (uncontended browser)
- **/event/2015new (champs division) — PASS**: Insights (118 Robonauts #1 norm 1955, 1678 Citrus
  Circuits #2 — 2015 championship winner present and sane), Figures (Top-16 EPA bars matching
  Insights; EPA-vs-Rank chart), Elim Matches (full bracket QF/SF/F with scores, score preds, win
  preds, video links). Closes the historical-tab coverage gap left by the event agent.
- **P2 NEW — 2015 ELIM matches missing winners → prediction accuracy + winner display wrong**:
  hist/1/event/2015new blob has `result.winner=None` for 14 of 16 elim matches (only f1m1/f1m2 have
  winner=red) despite decisive scores (e.g. qf1m1 217-150). Unlike QUALS (KNOWN legitimate — no
  head-to-head in 2015), 2015 elims DID have winners. Consequences: event "Match Predictions
  Accuracy: 12.5%" (correct calls counted as wrong), Win Pred cells shaded incorrect, match pages
  show blank "Actual Winner", and 2015-era elim W/L records undercount (254's 4-0-0 = finals-only).
  Likely inherited from TBA `winning_alliance` being empty for 2015 QF/SF rather than a migration
  bug (model could derive winner from scores); prod API unreachable (returns `{}`) so prod parity
  unconfirmed. Recommend: derive elim winner from scores for pre-winning_alliance years, or confirm
  prod shows the same and accept as upstream parity.
- **2021 crash root cause (code-level, exact)**: `frontend/src/constants.tsx:56` `RP_NAMES` map has
  2020 and 2022 keys but NO 2021; `frontend/src/pagesContent/teams/tabs.tsx` (BubbleChart
  columnOptions) evaluates `` `${RP_NAMES[year][0]}` `` whenever `year >= 2016` →
  `RP_NAMES[2021][0]` TypeError during render (before any data guard) → Next.js client-side
  exception. API side: /v3/site/team_years/2021 returns HTTP 500 body `{}`; hist blob 404 — but the
  crash is render-time and would occur regardless. /events?year=2021 doesn't touch RP_NAMES this
  way, hence graceful. One-line guard (skip RP columns when RP_NAMES[year] undefined, or add a 2021
  entry / year!==2021 condition) fixes it.

## PERFORMANCE INVESTIGATION — findings

### Cache-layer behavior (curl, direct)
- **Content-hashed immutable blobs** (`/v2/...<hash>`, `cache-control: public, max-age=31536000, immutable`):
  first hit at a CF PoP = `cf-cache-status: MISS` (origin GCS fetch ~180-400ms); subsequent = `HIT`
  (~30-60ms). Verified MISS→HIT→HIT→HIT on team_years/2026. So cold-PoP visitors pay origin
  latency per not-yet-warm blob; once warm they stay warm (immutable).
- **manifest.json = `cf-cache-status: DYNAMIC`** — Cloudflare does NOT cache it at all (JSON w/o a
  cache rule), despite origin `cache-control: public, max-age=60`. EVERY manifest fetch that isn't
  in the *browser's* own HTTP cache goes to GCS origin (~160-260ms). Browser HTTP cache (max-age=60)
  hides this on rapid repeat loads (saw 7-12ms), but a genuinely cold browser / >60s gap pays full.
- **API `/v3/site/*` (Cloud Run) = `cf-cache-status: DYNAMIC`**, `server: cloudflare` pass-through.
  Match endpoint: **1.72s cold, 0.34s warm** (warm-container repeat). Cold container / cold DuckDB
  query is the tail.

### Payload sizes (compressed blob on the wire)
- manifest.json: 55KB gzip / ~180KB raw (lists every blob's versioned path). Grows with catalog.
- team_years/2026 (full): **663KB** — downloaded on every team page (to find one row) AND teams-list.
- team/254: 995B. event/2026cmptx: 22KB. event/2026casnv small.

### Serialization / staircase analysis (Resource Timing, cold idb)
- **team page /team/254 (current year): 3-tier staircase** — manifest (ends ~158) → metadata wave
  `Promise.all([team_to_events, getTeam(254), getYearTeamYears(2026)])` (parallel, ends ~641, long
  pole = team_years/2026 481ms/663KB) → event wave `Promise.all(5 event blobs)` (parallel, ends
  ~1135). Two serial gates. Cold total data ≈ 1.0s. **The event wave waits on the WHOLE metadata
  Promise.all — including the 663KB team_years/2026 it does not need — before firing** (team.tsx:27
  then :33). Event blobs only depend on team_to_events (237ms), so ~240ms is wasted serialization.
- event page /event/2026casnv: 2-tier — manifest → [teams/all, events/all, event blob] parallel.
  No deep staircase. Cold ≈ manifest(210) + 1 event blob.
- match page /match/...: manifest (browser-cached) + **single Cloud Run API call** (checkBucket=false,
  match.tsx:8). No blob CDN. Dominated by Cloud Run: 260ms warm / 1.7s cold. This is the worst
  cold-case page class.
- teams-list /teams: 3-tier — manifest → team_years/2026.limit=100 (237ms) → full team_years/2026.
- historical team /team/254/2015: manifest → single `hist/1/team/254/2015` blob (260ms). Clean
  single-fetch. (Note: hist/ blobs are NOT content-hash-immutable pathed; cache behavior differs.)

### VERDICT on the serial-blob hypothesis
PARTIALLY CONFIRMED, with nuance. Blob reads DO fan out in parallel within each wave (Promise.all
present) — they are NOT one-at-a-time serialized. The real serial costs, ranked:
1. **manifest.json DYNAMIC (never CF-cached) gate** — a mandatory ~160-260ms origin RTT at the very
   start of every cold page, before any blob URL can be resolved. Most consistent contributor.
2. **CF MISS storms on cold PoP** — a page needing many immutable blobs pays origin latency on each
   not-yet-warm one; these fan out in parallel so it's max() not sum(), but the slowest MISS sets the
   floor. Transient (warms permanently after first visitor per PoP).
3. **Match pages bypass the CDN entirely** (checkBucket=false) → Cloud Run 1.7s cold. Dominant for
   the specific "match page felt slow" case.
4. **team-page event-wave gated on the 663KB team_years/2026** it doesn't need (~240ms avoidable).
5. **Large payloads**: 663KB team_years/2026 on every team + teams page; 180KB manifest.

### Remediation options (ranked by expected impact; NOT implemented)
- **[High] Make CF cache manifest.json** (Cache Rule / Cache-Everything on the blobs host for
  manifest.json, honor max-age=60, or edge-cache with 60s TTL). Removes the origin RTT from every
  cold page's critical path. Biggest single win, one config change.
- **[High] Match pages: serve from blob CDN** (add a match blob + checkBucket=true) OR keep-warm the
  Cloud Run container / precompute match blobs. Removes the 1.7s cold tail on the worst page class.
- **[Med] Decouple team event-wave from team_years**: gate event fetches on team_to_events only, not
  the whole Promise.all (team.tsx:27/33). Saves ~240ms/team page cold. Or fetch a slim per-team-year
  row blob instead of the 663KB full list.
- **[Med] Shrink/So team_years/2026**: team page needs one team's row — fetch `team/{t}/{year}` style
  slice rather than the 663KB league-wide list.
- **[Low] Warm CF cache** proactively (crawl top blobs post-deploy) to cut first-visitor MISS.

## Timings (cold, fresh idb; ms to last data-blob responseEnd; manifest state noted)

| Page class | URL | Cold data-load | Dominant contributor | Warm |
|---|---|---|---|---|
| team (current) | /team/254 | ~1135ms | 3-tier staircase; team_years 663KB 481ms + event wave | idb-cached, instant |
| team (historical) | /team/254/2015 | ~417ms | single hist blob 260ms | instant |
| event (current) | /event/2026casnv | ~450ms | manifest 210ms + event blob | instant |
| match | /match/2026cmptx_f1m1 | 260ms warm / ~1.7s cold Cloud Run | Cloud Run API (no CDN) | instant |
| teams-list | /teams | ~700ms | manifest + limit=100 list 237ms + full list | instant |

Note: manifest shows 160-260ms when browser-HTTP-cache-cold, 7-12ms when warm (max-age=60).
Absolute numbers are from a warm-ish CF edge (SJC); a cold PoP adds per-blob MISS latency (~200-400ms
each, parallel).

## Timings-raw

(see Findings for per-request waterfalls captured via Resource Timing)

---

## SEVERITY-ORDERED NEW FINDINGS (final)

- **P0 NEW — /teams?year=2021 client-side crash** ("Application error"). Root cause:
  `constants.tsx:56` RP_NAMES lacks a 2021 key; `pagesContent/teams/tabs.tsx` BubbleChart
  columnOptions does `RP_NAMES[year][0]` for year>=2016 → TypeError at render. Repro 3/3.
- **P1 NEW — literal "NaN" in Predicted Auto/Teleop/Endgame/Fouls on pre-component-era match pages**
  (verified /match/2015casj_qm1; 2019 clean → affected surface ≈ 2002-2015 matches). Should render
  "N/A" like sibling cells.
- **P2 NEW — 2015 elim matches missing result.winner (14/16 on 2015new)** → event prediction
  accuracy 12.5% nonsense, blank "Actual Winner", undercounted 2015 elim records. Likely upstream
  TBA-parity; needs prod comparison or score-derived winner.
- **P3 NEW — 2015 Insights "Ranking Score" column all 0.00** (data-consistent; hide or substitute
  year-appropriate metric).
- **P3 NEW (confirm intent) — elim match pages have no Prev/Next navigation** (quals do).

## PERFORMANCE VERDICT (final)

Serial-blob-read hypothesis: **PARTIALLY CONFIRMED — blobs fan out in parallel (Promise.all is
present everywhere it should be); the serialization that exists is (1) the manifest.json gate
(never CF-cached by design, ~160-260ms origin RTT at the head of every cold page) and (2) the
team-page 2-wave staircase (event wave gated on the full metadata Promise.all incl. an unneeded
663KB team_years blob, ~240ms avoidable).** The dominant cause of "occasionally slow page loads"
is most likely **(a) match pages bypassing the CDN entirely → Cloud Run cold ≈1.7s** (worst
observed; warm 260-340ms) and **(b) first-PoP-visitor CF MISS storms on immutable blobs**
(~200-400ms each, parallel so max() not sum(), transient until PoP warms). Payload note: the
663KB team_years/2026 blob is fetched on every team and teams page. See ranked remediation list
above (CF-cache the manifest with 60s TTL; blob-ify or keep-warm match data; decouple the team
event wave; slim team_years).

## COVERAGE STATEMENT

Exercised (real Chrome, staging): match pages 2026 quals/elims (2026casnv_qm1/f1m1,
2026cmptx_f1m1), historical 2015/2019 (2015casj_qm1, 2019casj_qm1) incl. Prev/Next + request-loop
regression; team pages 254, 1678, 2521 (mid-pack), 11247 (rookie), 9 (inactive-since-2008),
team-year 254/2015, and the multi-year Summary view (2002-2026 graph + Years Summary table); event
pages 2026casnv (ALL tabs incl. interactive Simulation reload ×3 and SOS), 2026cmptx, 2015casj,
2015necmp (district champs), 2015new (champs division: Insights/Figures/Elim); year switcher 2002/
2008/2015/2019/2021/2025 for teams+events lists with 2015 spot-check (254 rank 1 norm 2004
CONFIRMED); /matches noteworthy + upcoming; figures surfaces (team per-season EPA line, career
Norm-EPA line, event Top-16 bars, EPA-vs-Rank, bubble chart). Perf waterfalls cold+warm for team/
event/match/teams-list/historical-team page classes + curl header/cache analysis of blob CDN, 
manifest, and API. Gaps (honest): tooltips on charts not conclusively verified; team Figures metric
dropdown options not cycled; 2021 crash prevented teams-list-2021 content checks (crash IS the
finding); prod-parity for the 2015 elim-winner issue unconfirmed (prod API down); console capture
unavailable in tooling (relied on DOM/no-crash/NaN grep + visual).

---

## RESOLUTION (quality-perf-fixes agent, 2026-07-10 — deployed + verified live on staging)

Deployed to staging: api `statbotics-api-00010-ln6`, web `statbotics-web-00009-jv8`
(`staging` `eebb3e8` → `a4a9c8a`); CF blob worker updated. Smoke **9/9**. Full live
browser re-verification below.

- **P0 `/teams?year=2021` crash — FIXED.** `pagesContent/teams/tabs.tsx` BubbleChart
  `columnOptions` now guards the two RP columns on `RP_NAMES[year]` (undefined for the
  no-season year 2021) instead of `year >= 2016`, so the deref never runs and the page
  falls through to the graceful no-data state. LIVE `/teams?year=2021`: renders normal
  layout + tabs + "An error occurred, please try again later." — no "Application error",
  no blank page (2 loads). Branch `quality-fixes` (off master), draft PR chondl#12.

- **P1 pre-2016 match "NaN" — FIXED.** `pagesContent/match/[match_id]/table.tsx` treats a
  NaN alliance total the same as null → "N/A". LIVE `/match/2015casj_qm1`: Predicted
  Auto/Teleop/Endgame/Fouls = **N/A** (both alliances), Total Predicted = 28 / 89 (real),
  `document.body.innerText` contains no "NaN". Same branch/PR.

- **P2 2015 elim winners — FIXED IN CODE (backend); LIVE 2015 DATA BACKFILL DEFERRED.**
  `backend/src/tba/read_tba.py` derives the elim winner from alliance scores when TBA
  `winning_alliance` is absent, and restricts the 2015 winner-less rule to quals
  (`comp_level == "qm"`). Verified on the rig vs **live TBA** for 2015new + 2015casj:
  before → QF 8/8 + SF 6/6 winner-less (matches this audit's 14/16); after → all elim
  winners derived from score, all quals still winner-less. Same branch/PR (applies cleanly
  to master). The deployed api image carries the fix, but staging's 2015 event blobs
  (`hist/1/event/2015*`) + parquet were built from the old code, so live 2015 elim
  winners are unchanged until a targeted 2015 rebuild — documented in staging.md and
  deliberately not run (a single-year rebuild still mutates all 2015 records broadly; the
  fix is proven and any future historical rebuild applies it).

- **PERF-1 manifest edge-caching — FIXED + LIVE.** CF worker `statbotics-blob-proxy`
  `cacheEverything: true` for `/manifest.json` and rewrites the client `Cache-Control`
  back to `max-age=60` (Worker's own response header beats the zone's 4h Browser Cache TTL
  floor — verified). LIVE: repeat manifest fetches `cf-cache-status: HIT`, `age` advancing
  1→2→…→~60 then `REVALIDATED`; `max-age=60` preserved (not 14400); with-Origin variant
  also HITs; conditional GET → 304. Removes the ~160-260ms GCS RTT at the head of every
  cold page. Docs updated.

- **PERF-2 match pages off the API path — FIXED.** `frontend/src/api/match.tsx` derives the
  match view from the edge-cached `event/{key}` blob (parity confirmed: the event blob
  carries the match, its `team_matches` filtered by match key, and the event's
  `team_events` — exactly `MatchData`), API fallback preserved; no API supplement needed.
  LIVE `/match/2026cmptx_f1m1`: fetches `blobs.../v2/event/2026cmptx.4a47cd4b85d6`
  (immutable, 200), **zero** `/v3/site/match/` (Cloud Run) requests. Data-path
  before/after (2015casj_qm1): Cloud Run `/v3/site/match` = **41s cold-start /
  0.17-0.27s warm**; event blob via edge = **~80-90ms HIT**. On `bucket-first-serving`
  (PR #2), cascade-rebased stack, PR #2 body updated.

- **PERF-3 team event-wave — FIXED.** `frontend/src/api/team.tsx` starts the event-blob
  wave as soon as `team_to_events` resolves, no longer gated on the 663KB
  `team_years/{year}` blob in the metadata `Promise.all`. Same stack/PR.

### Accepted P3s (confirmed, no change)
- **2015 Insights "Ranking Score" column all 0.00** — model-consistent (2015 had no
  ranking-point system; teams ranked by Qual Average, quals winner-less). Cosmetic;
  ACCEPTED as-is (hiding/relabelling the column for pre-RP years is a future cosmetic
  nicety, not a correctness bug).
- **Elim match pages have no Prev/Next navigation** — quals expose adjacent-match arrows;
  elim brackets are non-linear (a match's "next" is ambiguous — winners advance across
  rounds), so the omission is a reasonable upstream design choice. ACCEPTED (intent
  confirmed by the bracket structure; no change).
