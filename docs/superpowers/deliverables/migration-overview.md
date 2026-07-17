# Statbotics: from outage-prone to fast and cheap to maintain

A crib sheet for the Chief Delphi post and the maintainer conversation. Everything below exists as working code: draft PRs on `chondl/statbotics` (fork), each verified on a local rig (CockroachDB + fake GCS + full 2026 season). The bug fixes and the first half of the migration stack (bucket-first serving + GC) additionally run live at https://statbotics.iterativerefinement.com (GCP + Cloudflare staging); the later stack PRs (state snapshot, DuckDB API, db-less mode) are rig-verified and not yet deployed to staging — say so plainly if asked. Nothing has been submitted upstream yet.

## The pitch in two sentences

Part 1: here are four small, independent bug fixes — each a handful of lines — for problems users have reported on Chief Delphi or hit every day without noticing. Part 2: here is a sequence of backend PRs, each independently valuable and shippable, that ends with statbotics serving faster than today while costing almost nothing to run and surviving the class of failure that has kept the site down since June.

## Why now (the evidence)

- Production has been hard-down since ~June 12: every DB-backed endpoint returns `500 {}` (issue #414, open for weeks). The app processes are healthy; the database layer is what died. Meanwhile the one component that never stopped working is the GCS blob bucket — the `/teams` page stayed up all summer serving stale blobs.
- In-season, users reported pages not loading on event weekends (CD thread, incl. API 500s during champs). The serving path is two F1 App Engine instances (min 0 → ~7.5s cold starts, max 2) in front of the same database the write pipeline contends with.
- Users reported wrong EPAs: the event-page team list showing a weeks-old EPA while the team page shows the correct one (CD, therekrab, 2026-04-12, with screenshots — root-caused below), and EPAs visibly cratering right after a match before "healing" minutes later.

---

# Part 1 — standalone bug fixes (no architecture change)

**UI fixes: match pages, SOS/Simulation, noteworthy ordering, duplicate fetches** *(fork PR #5)*
One consolidated PR for the user-facing frontend issues, one commit per fix. The match page rebuilt an unmemoized array every render, refiring a `useEffect` forever: one open match page issued **11,747 requests to TBA's media API in 5 seconds** (measured) — every production visitor has been silently doing this to TBA — and the Prev/Next buttons changed the URL but never loaded the new match (stale fetch guard). On the event page, the Strength-of-Schedule tab goes blank (or shows NaN) whenever all teams share the same start EPA — variance is 0 and the Gaussian library throws inside a web worker that never catches — and the Simulation EPA column read `epa.total_points.mean` where the API serves a plain number, so every row rendered 0. The Noteworthy Matches list ranked a null-score DQed placeholder #1 (CockroachDB sorts NULLs first under `DESC`; fixed with `.nullslast()`), and every blob was fetched twice per team page for lack of in-flight dedup. All verified before/after in a real browser. (The old 8s not-found debounce moved to Track 2 PR #2, where bucket-first serving makes the short 1.5s timeout safe.)

**Quality fixes** *(fork PR #12)*
Three small crashes from a staging audit: a `/teams?year=2021` client-side crash, literal "NaN" cells on pre-2016 match pages, and missing 2015 elim winners. Before/after verified against live TBA (`2015new`/`2015casj`).

**EPA consistency** *(fork PR #1 — 4 files, +49/−9)*
The user-visible bug: every model has a hand-written `__str__` listing "fields that matter," and both DB writes and blob uploads skip objects whose `__str__` didn't change. `Event.__str__` contains no EPA fields, so the event-page blob freezes at the last registration change — that's therekrab's screenshot, reproduced and fixed. The same lossy gate explains issue #413 (API disagreeing with the website: rank/percentile drift never reaches the DB). Fixes: gate on full-content equality (attrs `__eq__`, benchmarked at ~0.1s/cycle) and on rendered-payload hashes for blobs. Also included: matches whose score arrives before the TBA breakdown are deferred one cycle instead of ingesting zero components (the "our EPA dropped after we won" reports), and upsert batches are reduced to 250 rows — measured: 1,000-row team_years batches can exceed CockroachDB's default 16 MiB message cap and fail outright. Full-season replay output is SHA-256-identical before/after; cycle time unchanged (13.0–13.3s → 13.1–13.2s).

*(Fork PRs #3/#4 hold pytest suites for the above, kept out of the main PRs so those stay minimal; available if test infrastructure is wanted.)*

---

# Part 2 — the backend migration stack

Four PRs, each stacked on the last, each independently shippable and each leaving the system strictly better. The theme: the website already had a static-file serving layer (the GCS blobs) — these PRs finish that thought, then make the database progressively optional.

### Stack PR A: Bucket-first serving *(fork PR #2)*
Every team-page and event-page view served from GCS blobs, with the API as fallback only. Blobs become content-addressed (`v2/{path}.{hash}`) and immutable, resolved through a single `manifest.json` written *last* each cycle — so readers see an atomic, coherent set (no more mixed old/new data mid-publish), and edge/browser caching finally works (today's `?t=` cache-buster makes every page view a full origin fetch). A backfill script exports all historical seasons. Old and new frontend/backend deploy in either order.
**Evidence:** with every backend server stopped, a real Chrome session fully rendered event pages, team pages, and EPA charts from blobs alone — the current outage scenario, survived. Steady-state cycles upload **0 objects** (copy-on-write); a one-team change uploads 2 objects/42KB. FRC traffic is unusually CDN-friendly — an event's audience shares a venue and a CDN PoP, so the first spectator warms the cache for everyone: event-day load scales with matches played, not spectators.

### Stack PR B: Cleanup + manifest compression *(fork PR #7)*
A reference-aware GC endpoint (naive age-based lifecycle rules would delete still-referenced unchanged blobs; this keeps everything the current manifest references plus a 48h grace window for in-flight publishes) — run on a daily scheduler; demonstrated live (4,274 objects scanned, 324 unreferenced deleted, ~27MB freed, second run deletes 0). The manifest is gzip-encoded: 169KB → 53.5KB, the one object clients re-check every minute.

### Stack PR C: Pipeline state as a snapshot blob; DB off the hot path *(fork PR #9 — +213/−51)*
The pipeline never queries the database selectively — it loads the whole season at cycle start and diff-upserts at the end. This PR makes that explicit: state persists as one atomic compressed blob; the DB upsert moves *after* publish and becomes non-fatal.
**Evidence:** with CockroachDB stopped, a full cycle fetched TBA, computed EPA, and published blobs + snapshot — only a logged DB-write skip. When the DB returned, the content diff healed it automatically (deleted rows restored next cycle). This is precisely the June failure mode: with this PR, the site would have kept updating through the outage. State loads faster too (~1.4s vs ~2.7s).

### Stack PR D: Parquet + DuckDB for the public API *(fork PR #10 — +594/−6, behind an env flag, zero default change)*
Each cycle also exports the season as Parquet files; the `/v3` REST API can serve from in-process DuckDB over those blobs instead of the database (`API_BACKEND=duckdb`).
**Evidence:** 67,684 response fields compared between DB-backed and DuckDB-backed modes — zero mismatches. Faster nearly everywhere; offset pagination (what API scrapers hammer) went **322ms → 43ms p95**. Export costs ~2.3s/cycle. Future UI features become new SQL over existing blobs instead of new blob types.

### Stack PR E: retire the database *(fork PR #11 — +244/−37, behind an env flag, zero default change)*
Moves the last DB reads (TBA etags, cross-year EPA seeds, site-API fallback) onto the snapshot and Parquet blobs, behind `DISABLE_DB` that runs the entire system — pipeline, `/v3`, `/v3/site` — with no database at all.
**Evidence:** with CockroachDB stopped and `DISABLE_DB` set, a cold-started backend ran multiple full update cycles and served `/v3` + `/v3/site` with smoke 10/10; flipping the DB back on healed the deleted rows from the accumulated snapshot (3699 → 3724), and Parquet-seeded `get_init_epa` matched the DB seed to 0.00 across 60 teams.

## The end state

| | Today | After the stack |
|---|---|---|
| Website reads | 2 F1 instances + CockroachDB (7.5s cold starts, down since June) | GCS blobs + CDN; no runtime dependency on DB or API servers |
| Public API | CockroachDB queries | DuckDB over Parquet blobs (measured faster) |
| Update pipeline | DB as scratchpad; outage = total failure | In-memory + snapshot blob; DB outage = nothing (and eventually: no DB) |
| Consistency | Three cache layers, lossy publish gates, user-visible disagreements | One atomic manifest; content-gated publishes; blob == API verified to 0.0000 |
| Infra to operate | CockroachDB cluster + 3 App Engine services + bucket | 1 container service + 1 bucket (+ optional CDN hostname) |
| Idle cost | DB cluster + App Engine | Storage pennies; compute scales to zero |

Every claim above is reproducible: a local rig (`docs/superpowers/rig/`) with a one-command smoke suite, a parameterized cloud deploy script with a cloud-agnostic architecture doc (`docs/superpowers/rig/deploy/`), a Cloudflare setup guide written for the maintainer (`docs/superpowers/deliverables/cloudflare-bucket-proxy.md`), and a historical-data runbook (`docs/superpowers/deliverables/historical-backfill.md`).

## Suggested narrative for the CD post / maintainer message

1. Open with the diagnosis of the current outage (app healthy, DB layer down, blobs still serving) — it motivates everything without criticizing anyone.
2. Offer Part 1 as immediate, tiny, user-visible fixes — including the TBA request-flood fix, which the TBA folks will appreciate.
3. Present Part 2 as finishing the architecture the maintainer already started (he built the blob layer; these PRs complete it), framed by his own stated offseason goals: "simplify the setup, improve reliability."
4. Point at the staging site as proof — full history, live simulations, surviving DB shutdowns — and offer the PRs in whatever order and pace he prefers, no strings.

---

## Submission playbook (draft PRs on `chondl/statbotics`)

Two tracks, submitted in order: **Track 1 (bug fixes) first**, then **Track 2 (blob-store)**. Track 1 PRs are small, standalone, and independently verifiable — they build trust before the architectural ask. Track 2 is a linear stack; merged after Track 1 it hits exactly two documented conflicts (below), both with merge-verified resolutions. (The tracks are technically independent — the stack also applies cleanly on bare `master` — but Track-1-first is the plan; independence is a fallback if the maintainer wants the architecture sooner.)

### Track 1 — standalone bug fixes (recommended order `#5 → #12 → #1`)

Order is a suggestion (smallest/most-isolated first, biggest last); the three are mutually conflict-free and order-independent. Each can be taken or skipped on its own.

| # | Branch | One-liner to open with | Backing evidence |
|---|--------|------------------------|------------------|
| #5 | match-page-fixes | "The user-facing UI fixes in one PR: an unmemoized array makes every match page hammer TBA's media API (~11.7k requests in 5s) and Prev/Next never loads the new match; the Strength-of-Schedule tab goes blank on zero-variance EPAs and the Simulation EPA column reads 0; Noteworthy Matches ranks a DQed placeholder #1 (CockroachDB NULLS-FIRST); and team-page blobs fetch twice." | measured request count + real-browser before/after; rig null-ordering sim; staging |
| #12 | quality-fixes | "Three small crashes from a staging audit: `/teams?year=2021` client-side crash, literal 'NaN' cells on pre-2016 match pages, and missing 2015 elim winners." | before/after vs live TBA (`2015new`/`2015casj`); staging |
| #1 | epa-consistency | "The EPA-disagreement bugs: event pages freeze at the last registration change (therekrab's screenshot), the API disagrees with the site (#413), and EPAs crater when a score arrives before its breakdown. Full-season replay is SHA-256-identical." | rig reproductions per defect; #413; SHA-256 replay; staging |
| #4 | epa-consistency-tests | "Optional pytest suite for #1 (NaN-stability + deferral). Test infra only." | 9 passing tests |

### Track 2 — blob-store stack (order `#2 → #7 → #9 → #10 → #11`)

Stacked; each PR bases on the previous branch. Applies cleanly on bare `master`. Every PR is independently shippable and leaves the system strictly better.

| # | Branch | One-liner to open with | Backing evidence |
|---|--------|------------------------|------------------|
| #2 | bucket-first-serving | "Serve every team/event/match page from GCS blobs with the API as fallback only; blobs become content-addressed, immutable, and atomic behind a manifest-last publish. This is the outage the site survived, made the default." | API-down browser drill (24 bucket fetches, 0 API); torn-set drill; 0-object steady-state; staging |
| #7 | blob-gc | "Reference-aware GC for superseded `v2/` blobs (age-based lifecycle would delete still-referenced ones) + gzip the manifest (169KB → 53.5KB)." | live GC run (4,274 scanned, 324 deleted, ~27MB); gzip round-trip; staging |
| #9 | state-snapshot | "Persist pipeline state as one atomic snapshot blob and move the DB upsert after publish and make it non-fatal — the pipeline keeps updating through a DB outage. This is the June failure mode, survived." | DB-stopped full cycle; heal-on-return (3699→3724); ~1.4s vs ~2.7s state load (rig) |
| #10 | duckdb-api | "Publish the season as Parquet each cycle and serve `/v3` from in-process DuckDB over those blobs, behind `API_BACKEND=duckdb` (default unchanged). Faster nearly everywhere; pagination 322ms → 43ms p95." | 67,684 fields compared, 0 mismatches; latency table (rig) |
| #11 | db-retirement | "The last DB reads move onto snapshot + Parquet; `DISABLE_DB` runs pipeline + `/v3` + `/v3/site` with no database at all (default unchanged)." | db-less cold start, smoke 10/10; heal-on-return; Parquet-seed parity 0.00 (rig) |
| #3 | bucket-first-serving-tests | "Optional pytest suite for #2's publish logic. Test infra only." | 9 passing tests |

**Operational companion to #2:** once bucket-first serving is live, put the bucket behind a Cloudflare-proxied hostname so event-day traffic is absorbed at the edge. The dashboard-only walkthrough — including the exact Worker code (with manifest edge-caching), the zone Browser-Cache-TTL-floor gotcha, a curl verification checklist, and rollback — is `docs/superpowers/deliverables/cloudflare-bucket-proxy.md`. Free plan only; measured on staging: immutable blobs `cf-cache-status: HIT`, manifest edge-cached at 60s, match-page data ~85ms from the edge. The same doc covers optional free-plan **API rate limiting** at the edge (verified on staging: 60 req/10s per IP on `/v3`, excess blocked with 429) — relevant to the maintainer's interest in protecting the public API.

### Independence & taking things à la carte

- Any Track 1 PR can be merged alone. Any prefix of the stack (`#2`, then `#2+#7`, …) can be merged alone.
- The stack is **rig-verified end-to-end**; #2 and #7 additionally run on the staging site. #9/#10/#11 are rig-only (say so). #10/#11 are behind env flags with zero default change — zero-risk to merge as demonstrations.

### The one conflict, and its resolution

Cross-track only — appears if **both** Track 1 and the stack are merged:

- **`src/google/storage.py` — #1 vs #2.** Take **#2's** publish path. Its content-addressed manifest uploader carries an equivalent `nan_safe_eq` event-content gate that subsumes #1's blob gate. #1's other fixes survive untouched (they live in `data/utils.py`, `tba/read_tba.py`, `db/write/template.py`, `data/main.py`); `write_objs` has the same signature in both, so #1's reordered call binds cleanly. *Verified: a clean merge taking #2's file leaves all of #1's non-storage fixes intact.*
- **`frontend/src/api/storage.tsx` — #5 vs #2.** Take **#2's** rewrite; its `bucketInFlight` dedup already covers #5's double-blob-fetch symptom. Re-wrap `query()` in #5's `inFlight[storageKey]` pattern only if you also want query-level dedup.

(Fallback only: if the stack were taken **before** Track 1, there are **no conflicts** anywhere.)
