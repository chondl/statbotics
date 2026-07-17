# Spec: EPA Consistency (Track 1) and Bucket-First Serving (Track 2)

**Date:** 2026-07-09
**Status:** Approved for planning/implementation
**Supersedes in part:** `2026-04-11-duckdb-static-files-rearchitecture-design.md` (see "Relationship to the DuckDB design" below)

This spec defines two pull requests to be developed on the user's fork and reviewed by the user before anything is surfaced to the upstream maintainer. It captures verified root-cause analysis, agreed scope decisions, acceptance criteria, verification strategy, and hard process constraints. The implementing session should follow this spec; where it says "implementer decides," evaluate the listed options during planning and record the choice.

---

## 1. Background and verified facts

All facts below were verified against code and live systems on 2026-07-09. **Code baseline: upstream master `a2cea55` (2026-06-13).** The analysis was originally performed at `bd957af` (2026-04-07) and re-verified after fetching the maintainer's June rework (`bd957af..a2cea55`, 21 commits). That rework matters — see §1.7 for what changed upstream and what it means for these PRs. File:line references marked "(re-verified)" are current at `a2cea55`; treat any others as approximate and re-check during planning.

### 1.1 Deployment (confirmed)

- Backend runs on **Google App Engine** (confirmed via `server: Google Frontend` response header and `backend/deploy/*.yaml`):
  - `default` service (public `/v3` API) and `site` service (`/v3/site`): `instance_class: F1`, `min_instances: 0`, `max_instances: 2` (`api_app.yaml`, `site_app.yaml`). Observed cold start: ~7.5 s.
  - `data` service (ETL): `instance_class: B4`, `basic_scaling: max_instances: 1`, single gunicorn worker (`data_app.yaml`). **Updates are structurally single-writer.**
  - `dispatch.yaml` routes `/v3/data/*` → data, `/v3/site/*` → site, rest → default. `offseason_dispatch.yaml` drops the site route.
- Frontend is Next.js 13 on **Vercel, auto-deploys on push to upstream master**. No maintainer ops needed for frontend changes once merged.
- Serving DB is **CockroachDB** (SQLAlchemy 2.0, `CRDB_*` env vars).
- Update trigger is **external** (likely Cloud Scheduler; not in repo). It calls `/v3/site/update_curr_year` (`src/data/router.py:48`), which does a cheap TBA ETag pre-check and backgrounds a call to the data service's `/v3/data/update_curr_year`.

### 1.2 Production outage (context, not something these PRs fix)

- Since ~2026-06-12, every DB-backed endpoint on `api.statbotics.io` returns HTTP 500 `{}`; non-DB routes (`/`, `/info`) return 200. The app is healthy; the DB layer is down.
- GCS bucket `site_v1` blobs last-modified 2026-06-12 (pipeline stopped writing at the same time).
- Upstream issue **avgupta456/statbotics#414** (2026-06-15) reports this; no maintainer response as of 2026-07-09.
- On ChiefDelphi (2026-06-13) the maintainer (@Strategos) wrote: *"the website was broken today while I updated the database and server"* — the outage likely stems from an in-flight DB/server migration. **Implication: the maintainer may have local changes; keep PRs surgical and rebase-friendly.**
- Maintainer's stated offseason goals (CD post 2026-05-04): *"simplify the setup, improve reliability"*; remove offseason events; delete the Python API package. Frame PR descriptions in these terms.

### 1.3 Root cause of the user-visible EPA inconsistency (verified)

Users (including the maintainer's audience on CD — user therekrab, 2026-04-12, with screenshots: team 254 at `2026cancmp` showed EPA 311.5 on the event page vs 360.8 on the team page and teams list) see **wildly stale EPA on the event page's team list** while team pages are correct.

Verified chain:

1. Every update cycle — partial or full — replays **the entire season's EPA** (`src/data/epa/calc.py`, `process_year`: sorts all matches by time, processes each). At the end it explicitly refreshes TeamEvent EPA for teams with no matches played yet at an event (`calc.py` tail: "Records TeamEvent EPA stats if no matches played yet" → `post_record_team`, `src/models/epa/main.py:205-233`). **In-memory state is globally fresh and consistent every cycle.**
2. The DB write filter (the `changed()` helper in `src/data/utils.py write_objs`, ~line 57, re-verified) and the GCS blob upload filter (`src/google/storage.py`, `new_events` computation, re-verified) both gate on `str(old) != str(new)` using **hand-picked lossy `__str__` methods** (each commented "Only refresh DB if these change (during 1 min partial update)"):
   - `Event.__str__` (`src/db/models/event.py:88-98`, re-verified unchanged at `a2cea55`): key, status, num_teams, current_match, qual_matches — **no EPA fields**.
   - `TeamYear.__str__` (`team_year.py:119-123`): team, year, count, epa — omits rank, percentile, norm_epa, winrate.
   - `TeamEvent.__str__` (`team_event.py:121-132`): team, event, status, count, epa, rank — omits component EPAs, epa_sd, etc.
   - `Match.__str__` (`match.py:149-163`): omits most breakdown fields.
3. Consequence A (the reported bug): the `event/{key}` blob — the event page's data source (`frontend/src/api/event.tsx:5-10`, `checkBucket=true` → renders `data.team_events[].epa.*` in `frontend/src/pagesContent/event/[event_id]/insightsTable.tsx:67-81`) — is only re-uploaded when a match completes at that event or registration/status changes. During an event's registration window (weeks), teams' EPAs are refreshed in memory and in the DB (TeamEvent `__str__` includes `epa`) but the blob is never republished. The event page serves EPAs frozen at the last registration change.
4. Consequence B (upstream issue **#413**, "API data does not match website data"): rank/percentile/norm_epa drift never updates DB rows (omitted from `TeamYear.__str__`), so the public API (reads DB) disagrees with the website (reads blobs, and `team_years/{year}` blob is re-uploaded **unconditionally** every cycle, `storage.py:59`).
5. The lossy diffs exist to avoid CockroachDB write cost: the upsert writes **every non-PK column** on conflict (`src/db/write/template.py`, `on_conflict_do_update` with all columns; batched 1000 rows/statement). The performance workaround is the correctness bug. Note: in the June rework the maintainer consolidated all entity writes into a **single transaction per cycle** via `write_all` (`template.py:62-74`, re-verified) — but the lossy `changed()` gate in front of it is unchanged.

### 1.7 Maintainer's June rework (`bd957af..a2cea55`) — read before planning

The maintainer landed 21 commits (2026-04-14 through 2026-06-13) that touch the exact areas these PRs target. Verified highlights:

- **`api/` (PyPI package), `scripts/`, and `new/frontend/` are deleted** from the repo (his May 4 plan, executed).
- **TeamMatch is removed from the update pipeline** as an entity: dropped from the `objs_type` tuple, `read_objs`/`write_objs`, and `write_year`. **Correction from Track 2's re-inventory:** the `event/{key}` blob and `TeamYear` payloads still carry team-match data, now *derived* from `Match.pre_epas`/`epas` (see `agg.py`) instead of TeamMatch rows — the team-match figure views are live and derivable from existing blobs. `getTeamYears` (`/team/{num}/years`) has no frontend caller.
- **Write batching largely done upstream**: new `write_all` (`template.py:62-74`) executes all entity upserts in one `run_transaction`. Track 1's item D shrinks accordingly (see §2.2.D).
- **`DISABLE_GCS` kill-switch** added to both `backend/src/constants.py:38` and `frontend/src/constants.tsx:15` (currently `False`; commit "re-enable gcs"). Useful precedent: the maintainer wants runtime switches around the blob layer — Track 2 should preserve/extend this pattern.
- **`frontend/src/pages/apikey.tsx` added** — API-key authentication for the public API appears to be in progress (matches CD speculation). Do not build anything that assumes the public API stays unauthenticated.
- The lossy publish gates (`Event.__str__` etc.) and the sequential non-atomic blob uploads are **unchanged** — both tracks remain fully valid at `a2cea55`.

### 1.4 Second correctness bug: score-before-breakdown ingestion race (verified by user reports)

CD reports (Raysine 2026-04-12; Barnav 2026-05-02; Isaac-The-Pro 2026-05-01): TBA sometimes posts a match **score before the score breakdown**. The pipeline ingests the match as completed with all component values zero/absent, EPA components crater, then correct on a later cycle once the breakdown arrives. Transiently wrong EPA visible to users mid-event.

### 1.5 Read-path fragility relevant to Track 2

- Frontend fetch layer (`frontend/src/api/storage.tsx`): IndexedDB (TTL) → GCS bucket (if `checkBucket`) → API fallback. On total failure returns `undefined` (pages render broken). Expired IndexedDB entries are **deleted**, not used as last resort.
- Bucket-first coverage today (blobs written in `src/google/storage.py write_objs`): `teams/all`, `team_years/{CURR_YEAR}` (+`limit=100.metric=epa` variant), `events/all`, `events/{CURR_YEAR}`, `event/{key}` (gated, see above), `team_to_events`, `noteworthy_matches/{year}`, `upcoming_matches.limit=20.metric={...}`.
- **API-only fetches** (fail hard when API/DB is down; slow on F1 during event weekends):
  - `getTeam` → `/team/{num}` (`frontend/src/api/team.tsx:8-13`, `checkBucket=false`)
  - `getTeamYears` → `/team/{num}/years` (`team.tsx:92-97`)
  - `getTeamYear` → `/team/{num}/{year}` for non-current years (`team.tsx:88`; current year is reconstructed client-side from bucket blobs)
  - `getYearTeamYears` for **non-current years** (`teams.tsx:20`, `checkBucket` only when `year === CURR_YEAR`)
  - `getTeamYearTeamMatches` → `/team_year/{year}/{team}/matches` (`teams.tsx:22-31`)
  - `getTeamEventTeamMatches` → `/event/{event}/team_matches/{team}` (`event.tsx:12-20`) — since the June rework, the `event/{key}` blob **no longer** contains team_matches (§1.7); whether this view is still live upstream must be re-inventoried.
- Cache-busting bug/anti-pattern: bucket fetches append `?t=${Date.now() / 1000 / 60}` (`storage.tsx:61`) — **not floored**, so the query string is unique per request, defeating GCS/browser edge caching entirely. Every page view hits GCS origin.
- Blob uploads are sequential and non-atomic as a set: a reader mid-cycle can get a mix of old and new blobs (torn set). A crash mid-upload leaves the mix in place until the next cycle.

### 1.6 Other verified hazards (fix opportunistically only if touched)

- `@lru_cache(maxsize=None)` on `get_constants(year)` (`src/models/epa/init.py:16-22`) caches year score means in the long-lived B4 worker; means change early-season → stale seeds for teams initialized later in a warm process. A cycle-start cache clear is a one-liner.
- TBA ETags are persisted separately from the data they gate (split-brain on crash between writes).
- `alru_cache` on API/site routes: 2-min TTL, `max_size=8`; `no_cache=True` refreshes the shared entry (works as designed; not broken).
- TBA pickle cache has no TTL but `update_curr_year` passes `cache=False` for the current year (`src/data/main.py:155`) — not implicated in-season.

---

## 2. Track 1 — PR "EPA consistency" (branch: `epa-consistency`)

### 2.1 Goal

Every EPA value a user can see is, after each update cycle, consistent across the event page, team page, teams list, and public API — and no transiently-zero component EPAs appear when TBA posts scores before breakdowns.

### 2.2 Scope (user-confirmed: all four items in one PR)

**A. Content-based blob publish gate.** Replace the `str(Event)` gate in `src/google/storage.py write_objs` with a comparison of the **rendered blob payload** (hash of the serialized `_read_event(...)` output). Two candidate designs — implementer decides and records rationale:
   1. *Render-diff:* render payload for both `orig_objs` (DB state at cycle start) and `objs`; upload when they differ. Requires Track 1's item C (honest DB diff) so DB state converges to rendered truth, plus a **one-time full blob re-upload** on deploy (a `partial=False` run rewrites all blobs) to resync historically-stale blobs.
   2. *Persisted hashes:* store per-blob content hashes (e.g., a single `blob_hashes.json` object in the bucket, read at cycle start); upload when rendered hash differs from stored. Self-heals stale blobs automatically, no manual resync; one extra GCS read/write per cycle.
   - Either way: the decision to upload must depend on **what the blob would contain**, never on hand-picked model fields.

**B. Score-before-breakdown deferral.** For years ≥ 2016 (breakdown-bearing), a match whose TBA payload has a final score but a missing/null score breakdown is **not processed for EPA this cycle** (treated as still pending). Requirements:
   - No EPA update may consume imputed-zero components.
   - Matches must not get stuck: a fallback processes the match anyway after a bounded condition (implementer decides: age threshold such as 24 h, or event completed). Some events never post breakdowns; the fallback must cover them.
   - Match predictions (pre-match) remain published as today.
   - Locate the ingestion point in `src/data/tba.py` / `src/tba/` where completed-match status is assigned; the deferral must happen at ingestion or status level so the season replay naturally excludes the match.

**C. Honest DB write diff.** Replace the lossy `str()` comparison in the `changed()` helper (`src/data/utils.py write_objs`, ~line 57 at `a2cea55`) with **full-content equality** (the objects are `attrs` classes; compare `attr.asdict(curr) != attr.asdict(prev)` or equivalent). This closes issue #413's root cause. `__str__` methods may remain for logging but must no longer gate writes.

**D. Write efficiency, measurement-driven.** Item C increases written rows (rank/percentile drift across ~4K team_years that the lossy diff silently dropped). Note the maintainer's June rework already consolidated all entity writes into a single transaction per cycle (`write_all`, `template.py:62-74`) — the transaction-count batching originally envisioned here is **done upstream**. Remaining requirements:
   - Measure a partial-update cycle before and after item C (the pipeline's `Timer` prints per-step durations) on the local rig (§4).
   - Quantify the extra rows written by the honest diff.
   - Only if measurement shows a material regression, evaluate further efficiency work (chunk sizing; avoiding full-column upserts for unchanged columns is likely too invasive — evaluate, don't assume). Do not add speculative optimization to the PR.
   - Acceptance is "cycle time not materially regressed and quantified," not a promised speedup. Report honest numbers in the PR description.

### 2.3 Out of scope for Track 1

Versioned/atomic publishing (Track 2), new blob types (Track 2), frontend changes, the `lru_cache`/ETag hazards (§1.6) unless the diff already touches those lines.

### 2.4 Acceptance criteria

1. **Repro closed:** for an upcoming event (teams registered, no matches), after one update cycle the EPA for every team in the `event/{key}` blob equals that team's EPA in the `team_years/{year}` blob. (This is the therekrab/user-observed bug.)
2. **#413 class closed:** after a cycle where only rank/percentile/norm_epa change for some team, the DB row reflects the change (API and blob agree).
3. **Breakdown race closed:** a fixture match with score-but-no-breakdown produces no EPA change that cycle; once the breakdown appears (or the fallback condition triggers), it is processed exactly once with correct components.
4. **No stuck matches:** a match whose breakdown never arrives is eventually processed via the fallback.
5. **Performance quantified:** before/after cycle timings and row-write counts from the local rig are recorded in the PR description.
6. Full-season replay results are byte-identical to pre-change results for a fixture season with complete breakdowns (the changes affect *when* things publish, not *what* EPA computes).

---

## 3. Track 2 — PR "Bucket-first serving" (branch: `bucket-first-serving`)

### 3.1 Goal

Every view on team pages and event (competition) pages is served from GCS static blobs, with the API as fallback only — so event-weekend load never queues on the two F1 instances, and a database outage no longer breaks these pages. Blob sets become atomic and edge-cacheable.

### 3.2 Scope (user-confirmed: current year in-cycle + historical backfill script + versioned manifest)

**A. New blob exports** in `src/google/storage.py write_objs` (current year, every cycle, gated by the Track 1 content-hash mechanism):
   - `team/{num}` — payload of site `/team/{num}` (team info + all-years history; only teams whose payload changed get re-uploaded — bounded by teams that played since last cycle plus norm-EPA drift).
   - `team/{num}/years` — payload of site `/team/{num}/years`.
   - `team_year/{year}/{team}/matches` and `event/{event}/team_matches/{team}`: **re-inventory first** (§1.7) — the June rework removed team_matches from the pipeline and from the `event/{key}` blob, while the frontend still declares these API-only fetches. Determine whether these views are still live upstream, then decide between new blobs, folding into existing blobs, or explicitly leaving them API-backed; record the decision. Prefer deriving from existing blobs over minting thousands of tiny objects.
   - Reuse the site routers' existing `_read_*` shaping helpers so blob payloads are byte-identical to API responses (the frontend must not care which source it hit).

**B. Historical backfill script** (one-time, maintainer-run): exports for each past year the blobs needed by historical views — `team_years/{year}`, `events/{year}`, `event/{key}` per event, and per-team year payloads (`team/{num}/{year}`) — using the same `_read_*` helpers. Deliver as a script or data-service endpoint consistent with repo conventions (`src/data/router.py` has precedent for admin endpoints). Must be idempotent and resumable. Note: 2021 has no season.

**C. Versioned, atomic publishing (user-confirmed requirement).** Replace the per-request `?t=` cache-buster with manifest-driven versioning:
   - A `manifest.json` written **last** each cycle, after all blob uploads succeed. Readers resolve blob URLs through the manifest, so they always see a coherent set (old or new, never mixed), and a crash mid-upload leaves the old manifest pointing at the old coherent set.
   - Blob URLs referenced by the manifest must be **immutable** (content-addressed path or hash query param) with long `Cache-Control`, so GCS/browser edge caching absorbs event-weekend load. Unchanged blobs must **not** be re-uploaded per version (copy-on-write by content hash).
   - The manifest itself is fetched with a short TTL (~60 s).
   - Storage growth bounded: include a GCS lifecycle rule recommendation (delete unreferenced versions after a few days) in the PR description.
   - Exact addressing scheme (per-blob hash paths vs versioned prefixes for changed blobs only) is implementer's choice; requirements above are binding.

**D. Frontend changes** (`frontend/src/api/`):
   - Bucket-first (`checkBucket=true` semantics) for all team-page and event-page fetches listed in §1.5, all years — via the manifest when present.
   - **Deploy-ordering safety:** Vercel auto-deploys the frontend the moment upstream master merges, but the maintainer deploys App Engine manually and may lag. The new frontend must work against a backend that has never written a manifest (fall back to today's paths + `?t=` behavior), and the old frontend must keep working against a backend that writes manifests (keep writing legacy paths during a documented transition, or design paths to be backward-compatible).
   - **Stale-if-error:** when both bucket and API fail, serve the expired IndexedDB entry instead of deleting it and rendering a broken page (`storage.tsx getWithExpiry` currently deletes).
   - Remove the client-side current-year team-page reconstruction fallback only if the new `team/{num}` blobs fully cover it; otherwise leave it.

### 3.3 Out of scope for Track 2

The public `/v3` REST API remains DB-backed (external consumers). No platform migration, no DuckDB, no changes to `new/frontend/`, no changes to update triggering.

### 3.4 Acceptance criteria

1. With the local rig's API stopped (simulating the current outage), team pages and event pages fully render from blob storage (current and historical years).
2. A torn-set scenario (kill the publisher between blob upload and manifest write) leaves readers on the previous coherent set; the next cycle publishes normally.
3. Every blob URL the frontend fetches (except the manifest) is immutable and served with long-lived `Cache-Control`; the manifest is the only short-TTL fetch. No `Date.now()`-style busters remain on bucket paths.
4. Old frontend + new backend and new frontend + old backend both function (deploy-ordering matrix).
5. Blob payloads are byte-identical to the corresponding site API responses for the same data.
6. Upload volume per cycle is measured and reported (objects written, bytes) at peak-season fixture scale.

### 3.5 Branch relationship

Both branches cut from `master`. Track 2's publish mechanism generalizes Track 1's blob gate; expect overlap in `src/google/storage.py`. Keep Track 2 rebase-ready onto Track 1 (the maintainer may accept them in either order); if the planner prefers, Track 2 may be based on Track 1 with that dependency stated in the PR description.

---

## 4. Verification strategy (user-confirmed: local end-to-end)

Production cannot be used (DB down; no GCP/CockroachDB credentials). Build a local rig:

- **CockroachDB** locally (docker or brew; dev conn string in `backend/CLAUDE.md`: `cockroachdb://root@localhost:26257/statbotics3?sslmode=disable`, `LOCAL_DB=true` path in `src/constants.py` — verify).
- **TBA API key:** check `backend/src/constants.py` for the env var; a public read key exists in `frontend/src/constants.tsx` (`TBA_API_KEY`) usable if no other is available. 2026 season data is complete/static — good fixtures.
- **GCS substitute:** no bucket credentials exist. Use `fake-gcs-server` (docker) or a filesystem shim injected at `src/google/storage.py`'s client construction. Do not ship shim code in the PR paths that run in production; keep it in tests/scripts.
- Seed data: full `reset_all_years` takes hours; a current-year-only build (`update_curr_year(partial=False)`) with empty prior years is sufficient for mechanics (EPA seeds differ from prod — fine for gate/diff/publish verification, NOT for asserting absolute EPA values). For acceptance 2.4.6 (byte-identical replay), compare pre/post-change runs of the same local build, not prod values.
- Tests: the repo has **zero test infrastructure**. Include minimal, dependency-light pytest coverage for the new pure logic (content-hash gate, honest diff, breakdown-deferral policy, manifest read/write). Keep the footprint small — this is a credibility asset, not a test-infra PR. If adding pytest to packaging conflicts with the maintainer's in-flight rework, an isolated `backend/tests/` + `requirements-dev.txt` addition is acceptable.
- Capture for each PR description: before/after cycle timings, row/blob write counts, and the acceptance-criteria evidence.

### Shared smoke suite (user-requested)

A scripted smoke test — not a unit-test framework — lives with the rig at `docs/superpowers/rig/smoke/` and must be runnable by any agent against any environment via a base URL + bucket location (local rig, Postgres stack, staging later). Checks, each with clear pass/fail output and non-zero exit on failure:
1. Liveness: `/` and `/info` return 200.
2. DB-backed reads: representative `/v3` and `/v3/site` endpoints return 200 with non-empty, sane payloads (e.g., `/v3/team/254` has `team == 254` and EPA fields).
3. Blob reads: `teams/all`, `team_years/{CURR_YEAR}`, and one `event/{key}` blob fetch, zlib-decompress, and parse with non-trivial content.
4. Consistency probe (regression guard for the Track 1 bug): a sampled team's EPA in an `event/{key}` blob matches the same team's EPA in `team_years/{year}` within tolerance.
5. Optional (flag-gated, slower): trigger one update cycle and assert success and that blob timestamps/versions advanced.

Agents run the smoke suite after any significant change and before declaring acceptance criteria met; its output belongs in the evidence captured for PR descriptions.

---

## 5. GitHub process (user-confirmed; hard constraints)

- Fork exists: **`github.com/chondl/statbotics`** (public; forks cannot be private; no upstream notification occurs).
- Remotes (configured): `origin` = `avgupta456/statbotics` (pull only — never push), `fork` = `chondl/statbotics` (all pushes).
- Branches (created from `a2cea55`, pushed to fork with tracking): `epa-consistency`, `bucket-first-serving`.
- Worktrees (created; ignored via `.git/info/exclude`, deliberately not `.gitignore` to keep PR branches clean):
  - `/Users/chondl/learn/statbotics/.worktrees/epa-consistency` (Track 1)
  - `/Users/chondl/learn/statbotics/.worktrees/bucket-first-serving` (Track 2)
  - Note: `docs/` (this spec) lives untracked in the **main checkout only** — read it via absolute path from the worktrees; do not commit it to the PR branches.
- PRs are opened **on the fork only**: `gh pr create --repo chondl/statbotics --base master --head <branch>` — the `--repo` flag is mandatory every time (gh's default for forks targets upstream).
- **Never** open PRs, issues, or comments on `avgupta456/statbotics`. **Never** post to ChiefDelphi. The user personally writes the issue #414 comment and CD posts after reviewing the PRs; upstream PRs happen only on the user's explicit go.
- PR descriptions should be written to be reusable upstream: problem statement citing user-visible symptoms (therekrab's CD post with screenshots for Track 1; May 1 API 500s and June outage for Track 2), root cause with file:line, measured evidence, and framing aligned with the maintainer's stated goals ("simplify the setup, improve reliability"). No time estimates.

## 6. Relationship to the DuckDB design doc

The 2026-04-11 design remains the reference for a full re-architecture, but these PRs deliberately take the opposite stance on two of its decisions, based on evidence gathered since:

- It proposed dropping the GCS blob layer and serving all reads from the API. The June outage demonstrated the blob layer is the resilient half (it alone kept serving); Track 2 doubles down on blobs instead.
- It proposed incremental EPA with checkpoint/resume. Track 1 keeps the existing deterministic full-season replay (the robustness stance — no incremental state to corrupt) and fixes the publish gates around it.

Adopted from the doc: single-writer discipline, atomic manifest-last publishing, serve-stale-on-failure. The DuckDB storage swap remains a possible future step, isolated behind the now-fixed serving contract; it is not part of these PRs.

## 7. Known risks / open questions for the planner

1. Maintainer's rework is **active and landing** (§1.7: 21 commits through 2026-06-13, including deletions of whole directories and pipeline restructuring). Before implementation starts, `git fetch origin` and re-verify the inventory in §1.5/§1.7; keep diffs surgical, avoid gratuitous refactors, rebase before surfacing upstream. Both branches were cut from `a2cea55`.
2. The exact TBA payload shape for "score posted, breakdown missing" needs confirmation from fixtures (is `score_breakdown` null, absent, or partially populated?).
3. Per-team blob churn/cost at peak season: estimate object writes/cycle under the honest gate (bounded by teams whose rendered payload changed) and report; if norm-EPA drift causes near-universal churn, consider splitting slow-moving fields out of the per-team payload.
4. Whether `getTeamYear` current-year client-side reconstruction is retired or kept (§3.2.D).
5. `attrs` equality performance on ~200K objects per cycle (item C) — measure; hash caching is an easy fallback.
6. The external scheduler is invisible to the repo; nothing in these PRs may assume its cadence beyond "cycles run repeatedly."
