# Trailing-Year Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the trailing year's events refreshing and publishing like the
current year until Dec 28 00:00 PT, and give past years per-year hist epochs so
their pages can never stay stale.

**Architecture:** One module owns the trailing-window rule and the live-year
predicate. The manifest gains `trailing_year` and `hist_epochs` and every write
goes through one read-mutate-conditional-write helper. Trailing cycles publish
versioned blobs through the manifest; past-year reprocesses write hist blobs
under a year epoch that advances on any change; window close turns the
trailing year into an ordinary past year.

**Tech Stack:** Python 3.11, FastAPI, google-cloud-storage, pytest; Next.js 13
frontend (TypeScript), `node --test` (Node 24 runs `.ts` directly).

**Spec:** [2026-11-24-trailing-year-refresh-design.md](../specs/2026-11-24-trailing-year-refresh-design.md)

## Global Constraints

- Trailing year = `CURR_YEAR − 1`. Trailing window open ⇔ now < Dec 28 00:00 `America/Los_Angeles` of the trailing year (2026-12-28 08:00 UTC for 2026).
- Live year = `CURR_YEAR`, or the trailing year while the trailing window is open.
- No hist blob is ever written for a live year (enforced in code).
- Every `manifest.json` write uses `if_generation_match`; on 412 re-read, re-apply, retry up to 5 times, then raise.
- `read_manifest` returns `None` only for "object not found"; any other failure raises `ManifestReadError`, and writers abort.
- Hist resolution: a manifest entry wins; else `hist/{max(hist_epochs[year] ?? 0, hist_epoch)}/{path}`.
- Year-from-path: `event/{key}` → first 4 digits of key; `events/Y…`, `team_years/Y…`, `noteworthy_matches/Y…` → `Y`; `parquet/Y/…` → `Y`; `team/{num}/Y` → `Y`; anything else → no year.
- Tests: `PY=/tmp/claude-1000/-repos-popcornpenguins-statbotics/a31463e1-e814-4c6a-8829-d9fb2483bbd1/scratchpad/venv/bin/python` then `cd backend && $PY -m pytest tests -q` (baseline 199 passed). Frontend: `node --test frontend/tests/*.test.ts` and `cd frontend && yarn build`.
- Terms exactly as spec §3: trailing year, trailing window, live year, event date window, trailing cycle, trailing publish, window close, year epoch, site blobs, year keys, manifest precondition.

**Deviation from spec §7 step 1 (recorded, spec amended in Task 9):** the final
full trailing cycle at window close runs with **past-year** semantics, not
live-year. After close the year is an ordinary past year; computing it the way
the sweep will later compute it avoids a spurious year-epoch advance on the
next sweep visit. All trailing-year events end by Tue 12-08, so statuses agree.

## Review Focus

1. **Manifest read failure mid-cycle** — a transient GCS error must abort the publish, never write a manifest planned from empty (would erase `hist_epochs`). Test in Task 2.
2. **Two writers racing** — a current-year publish and a trailing publish interleaving must both land. Test in Task 2.
3. **Cold year under a fresh epoch** — `reset_all_years` on an empty bucket must gap-fill under the global epoch, not advance every year. Test in Task 5.
4. **Gate tick exactly at the close instant** — 2026-12-28 07:59:59 UTC is open, 08:00:00 is closed. Test in Task 1.
5. **Frontend with an old manifest** (no `hist_epochs`, no `trailing_year`) — resolves exactly as today. Test in Task 8.

---

### Task 1: Trailing-window rule and live-year predicate

**Files:**
- Create: `backend/src/data/trailing.py`
- Test: `backend/tests/test_trailing_window.py`

**Interfaces:**
- Produces: `trailing_year() -> int`; `trailing_window_open(now: Optional[datetime] = None) -> bool` (aware UTC datetime; default `datetime.now(timezone.utc)`); `is_live_year(year: int, now: Optional[datetime] = None) -> bool`.

- [ ] **Step 1: Write failing tests** — `test_open_before_close` (2026-12-28T07:59:59Z → True), `test_closed_at_close` (08:00:00Z → False), `test_open_at_rollover` (2026-11-06T07:00Z → True), `test_live_years` (`is_live_year(CURR_YEAR)` always True; `is_live_year(CURR_YEAR-1, open)` True, closed False; `is_live_year(CURR_YEAR-2)` False). Monkeypatch `trailing.CURR_YEAR = 2027`.
- [ ] **Step 2: Run** `$PY -m pytest tests/test_trailing_window.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement** with `zoneinfo.ZoneInfo("America/Los_Angeles")` resolved at import (a missing zone database fails import, which fails startup).
- [ ] **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(trailing): trailing-window rule and live-year predicate`.

### Task 2: Manifest model and integrity

**Files:**
- Modify: `backend/src/google/publish.py` (Manifest, UploadPlan, plan_uploads)
- Modify: `backend/src/google/storage.py:85-114` (read_manifest, write_manifest, _publish), `:317`, `:356-370` (publish_team_artifacts)
- Modify: `backend/src/google/parquet.py:65-90` (write_parquet)
- Create: `backend/src/google/year_path.py`, `backend/tests/data/year_from_path_vectors.json`
- Test: `backend/tests/test_manifest_integrity.py`, `backend/tests/test_year_path.py`

**Interfaces:**
- Produces:
  - `Manifest` fields `trailing_year: Optional[int] = None`, `hist_epochs: Dict[str, int] = {}`; `to_json`/`from_json` round-trip them.
  - `UploadPlan` becomes `uploads`, `legacy_uploads`, `blob_updates: Dict[str, str]`, `blob_removals: Set[str]` (no whole manifest). `plan_uploads(rendered, prev, cycle)` fills `blob_updates` only for changed digests and keeps `hist_epoch` untouched.
  - `year_from_path(path: str) -> Optional[int]` in `year_path.py`; `effective_hist_epoch(manifest: Manifest, year: Optional[int]) -> int`.
  - `class ManifestReadError(Exception)`; `read_manifest() -> Optional[Manifest]` (None only on `google.api_core.exceptions.NotFound`).
  - `update_manifest(mutate: Callable[[Manifest], None], bucket=None) -> Manifest`: reads manifest + generation (`bucket.get_blob`), applies `mutate` to a fresh copy, stamps `cycle` (UTC iso) and `hist_epoch = max(prev.hist_epoch, HIST_EPOCH)`, drops `hist_epochs` entries `<=` the global epoch, writes with `if_generation_match` (0 when absent), retries on `PreconditionFailed` up to 5 times, then raises.
  - `_publish(plan)`, `publish_team_artifacts`, `write_parquet` all commit through `update_manifest`, applying `blob_updates`/`blob_removals` to the fresh manifest.
- Vectors JSON: list of `{"path": str, "year": int|null}`, at least: `event/2026mmr`→2026, `events/2026`→2026, `team_years/2026.limit=100.metric=epa`→2026, `noteworthy_matches/2026`→2026, `team/6238/2026`→2026, `team/6238`→null, `teams/all`→null, `events/all`→null, `team_to_events`→null, `upcoming_matches.limit=20.metric=max_epa`→null, `parquet/2026/team_years`→2026.

- [ ] **Step 1: Write failing tests**
  - `test_year_from_path_vectors` (loads JSON).
  - `test_manifest_round_trips_new_fields`.
  - `test_plan_uploads_keeps_unknown_state` (prev with `trailing_year=2026`, `hist_epochs={"2026":3}` → `update_manifest` result keeps both).
  - `test_update_manifest_retries_on_412` (fake bucket: first write raises `PreconditionFailed`, a concurrent writer's key appears; final manifest holds both changes).
  - `test_read_error_aborts_write` (download raises a non-NotFound error → `ManifestReadError`, no upload attempted).
  - `test_effective_epoch` (`max` rule, entries at/below global dropped on write).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement** per Interfaces. The fake bucket for tests lives in `backend/tests/fake_gcs.py` (blobs dict with generation counters, `get_blob`, `blob().upload_from_string(..., if_generation_match=)`, `download_as_bytes`, `md5_hash` as base64 md5 of stored bytes, `exists`); later tasks reuse it.
- [ ] **Step 4: Run full suite** → all pass (existing publish tests updated to the new `UploadPlan` shape, not weakened).
- [ ] **Step 5: Commit** `feat(manifest): trailing/epoch fields, conditional writes, fail-closed reads`.

### Task 3: Live-year semantics in the pipeline and ping

**Files:**
- Modify: `backend/src/data/tba.py:92` (`get_event_status`), `:303` (`full_curr_year` → `full_live_year`)
- Modify: `backend/src/data/router.py:136` (ping key check)
- Test: `backend/tests/test_live_year_status.py`, `backend/tests/test_ping.py`

**Interfaces:**
- Consumes: `is_live_year` (Task 1).
- Classification of every other `CURR_YEAR` use in `backend/src` (from `grep -rn CURR_YEAR backend/src`), recorded as a comment block at the top of `trailing.py`: current-year-only — `breakdown.py`, `tba/breakdown.py`, `data/teams.py`, `db/models/team_year.py:178`, `data/epa/main.py:43`, `data/wins.py:187`, `site/team_year.py:22`, `api/query.py:55`, `google/storage.py` (`write_objs`, `publish_team_artifacts`), `db_duckdb/main.py`, `data/main.py` current-year paths. Live-year — the two `tba.py` sites and the ping.

- [ ] **Step 1: Write failing tests** — `test_trailing_event_without_matches_is_upcoming`, `test_trailing_event_with_quals_only_is_ongoing`, `test_past_year_event_without_matches_is_invalid` (window closed), `test_ping_accepts_trailing_key_while_open` (202), `test_ping_rejects_trailing_key_after_close` (204). Freeze time by monkeypatching `trailing.trailing_window_open`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**: `get_event_status` tests `is_live_year(year)`; `full_live_year = not partial and is_live_year(year_num)`; ping accepts `rf"({CURR_YEAR}|{trailing_year()})[a-z0-9]+"` only when the year is live.
- [ ] **Step 4: Run full suite** → pass.
- [ ] **Step 5: Commit** `feat(trailing): live-year event statuses and ping keys`.

### Task 4: Render-only site blobs, deterministic

**Files:**
- Modify: `backend/src/google/storage.py:425-515` (`write_hist_blobs`)
- Test: `backend/tests/test_hist_blobs.py`

**Interfaces:**
- Produces: `render_site_blobs(year: int, objs: objs_type, teams: List[Team]) -> Dict[str, bytes]` — compressed payloads for exactly the four site-blob families, with every input list sorted by key (`team`, `key`, `event`+`team`, match `key`) before rendering. `write_hist_blobs` keeps its signature, calls `render_site_blobs`, and raises `ValueError("live year")` when `is_live_year(year)`.

- [ ] **Step 1: Failing tests** — `test_render_is_order_independent` (same objs with dicts in reversed insertion order → identical bytes per path); `test_write_hist_blobs_refuses_live_year`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** → pass.
- [ ] **Step 5: Commit** `refactor(hist): render-only site blobs with sorted inputs`.

### Task 5: Year epochs for past years

**Files:**
- Create: `backend/src/google/year_epoch.py`
- Modify: `backend/src/data/main.py:150-163` (past-year branch of `process_year`)
- Test: `backend/tests/test_year_epoch.py`

**Interfaces:**
- Consumes: `render_site_blobs` (Task 4), `update_manifest`, `effective_hist_epoch`, `year_from_path` (Task 2), `fake_gcs` (Task 2).
- Produces: `publish_past_year(year: int, rendered: Dict[str, bytes], bucket=None) -> str` returning `"unchanged" | "gap_filled" | "advanced"`:
  - `e = effective_hist_epoch(manifest, year)`; look up each path's stored md5 under `hist/{e}/` (`bucket.get_blob`, thread pool).
  - All missing → upload under `e` (gap fill, `IMMUTABLE_CACHE`), no manifest change → `"gap_filled"`.
  - Any stored md5 differs or some missing → upload every path under `e+1`, overwriting, then `update_manifest` sets `hist_epochs[str(year)] = e+1` → `"advanced"`.
  - Otherwise `"unchanged"`.
  - In every outcome, the same `update_manifest` call (or one alone when nothing else changes) removes the year's non-Parquet year keys from `blobs` (they would shadow the fresh hist blobs).
- `process_year` past-year branch: `write_parquet` then `publish_past_year(year, render_site_blobs(...))`, skipped entirely for a live year.

- [ ] **Step 1: Failing tests** — `test_unchanged_year_writes_nothing`; `test_changed_path_advances_whole_year` (all paths written under e+1, `hist_epochs` set after uploads); `test_cold_year_gap_fills_under_global_epoch` (Review Focus 3); `test_removes_shadowing_year_keys_but_keeps_parquet`; `test_advance_is_rerunnable_after_crash` (crash after uploads, before manifest write → rerun reaches `"advanced"` with the same e+1).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** → pass; existing `test_hist_blobs.py` / `test_tba_sweep.py` updated only where they asserted "skip existing".
- [ ] **Step 5: Commit** `feat(hist): per-year epochs; past-year reprocesses can never leave pages stale`.

### Task 6: Trailing cycle and trailing publish

**Files:**
- Modify: `backend/src/data/main.py` (new `update_trailing_year`, `close_trailing_year`; `process_year` branch for a live non-current year)
- Create: `backend/src/google/trailing_publish.py`
- Modify: `backend/src/data/sweep.py:36-49` (`normalize_cursor` skips a live year)
- Test: `backend/tests/test_trailing_cycle.py`, `backend/tests/test_tba_sweep.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4, 5.
- Produces:
  - `trailing_publish(year: int, objs, teams, bootstrap: bool) -> bool` — renders site blobs + `build_parquet_uploads(year, …)`, uploads changed versioned objects, then one `update_manifest`: set changed `blob_updates`; if `bootstrap`, remove the year's year keys that were not rendered (Parquet keys excepted); set `trailing_year = year`. Returns whether the `parquet/{year}/team_years` digest changed.
  - `update_trailing_year(full: bool) -> None` in `main.py`: partial path reads `read_snapshot(year)`, falls back to full if unreadable; runs `process_year(year, partial, partial, True, teams, objs, None)` (teams from snapshot, or `load_teams_tba(cache=True)` with `create_objs(year)` for full); `process_year`'s live-non-current branch calls `trailing_publish(..., bootstrap=full)` then `write_snapshot(year, objs, teams)`; when it returns True, chain `update_curr_year(partial=False, tba_partial=False)` and `refresh_teams()`. Module state `trailing_last_cycle: Optional[str]`, `trailing_last_error: Optional[str]` for `/info`.
  - `close_trailing_year() -> str` in `main.py`: no-op `"not_needed"` unless the window is closed and (`manifest.trailing_year` is set or the trailing year still has non-Parquet year keys); else a full `process_year` with past-year semantics (Task 5's branch runs `publish_past_year`), then `update_manifest` sets `trailing_year = None` → `"closed"`. Stateless and rerunnable.
- `normalize_cursor` advances past any `is_live_year` year.

- [ ] **Step 1: Failing tests** (fake GCS; monkeypatched `process_year_tba` returning fixture objs):
  - `test_bootstrap_publishes_and_stamps_marker`;
  - `test_bootstrap_removes_unrendered_year_keys` (`team_years/2026.limit=100.metric=epa`, `noteworthy_matches/2026` gone; `parquet/2026/...` kept);
  - `test_trailing_publish_is_hash_gated` (second identical publish uploads nothing);
  - `test_trailing_cycle_writes_no_hist`;
  - `test_chain_only_when_team_years_digest_changes`;
  - `test_one_manifest_write_per_trailing_publish`;
  - `test_close_not_needed_while_open`, `test_close_retires_year` (year keys gone except Parquet, `trailing_year` None, `hist_epochs` advanced when hist pre-existed), `test_close_rerun_after_crash`;
  - `test_sweep_skips_live_trailing_year`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** → pass.
- [ ] **Step 5: Commit** `feat(trailing): trailing cycles, trailing publish, window close`.

### Task 7: Gate, endpoints, `/info`

**Files:**
- Modify: `backend/src/data/router.py` (`update_curr_year_site_endpoint`; new data endpoints)
- Modify: `backend/main.py:73` (`/info`)
- Test: `backend/tests/test_trailing_gate.py`, `backend/tests/test_ping.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 6.
- Produces:
  - `trailing_gate_action(manifest: Optional[Manifest]) -> Optional[str]` in `router.py`: `"close"` when `close_trailing_year` would act; `None` when the window is closed; `"full"` when `manifest.trailing_year != trailing_year()`; else read the trailing snapshot (`"full"` if unreadable) and `check_year_partial_tba(year, event_objs, etags)` → `"partial"` or `None`.
  - `GET /v3/data/update_trailing_year?full=` → `update_trailing_year(full)`; `GET /v3/data/close_trailing_year` → `close_trailing_year()`.
  - The site gate runs the current-year probe as today, then the trailing action; each positive result adds its own background self-call. Response: `{"status": ..., "trailing": <action or null>}` (current-year `status` values unchanged).
  - `/info` gains `TRAILING_YEAR`, `TRAILING_WINDOW_OPEN`, `TRAILING_LAST_CYCLE`, `TRAILING_LAST_ERROR`, `HIST_EPOCHS`.

- [ ] **Step 1: Failing tests** — `test_gate_bootstraps_when_marker_absent`, `test_gate_partial_on_change`, `test_gate_idle_when_unchanged`, `test_gate_closes_after_window`, `test_gate_ignores_trailing_after_close_done`, `test_current_year_behaviour_unchanged` (existing `test_ping.py` gate tests still pass with `"trailing"` added).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** → pass.
- [ ] **Step 5: Commit** `feat(trailing): gate probes the trailing year; endpoints and /info`.

### Task 8: Frontend live year and per-year epochs

**Files:**
- Create: `frontend/src/api/yearFromPath.ts` (no imports), `frontend/tests/yearFromPath.test.ts`
- Modify: `frontend/src/api/storage.tsx` (Manifest interface, `resolveBucketUrl`, export `isLiveYear`)
- Modify: `frontend/src/pages/event/[event_id].tsx:49`, `frontend/src/api/events.tsx:9`, `matches.tsx:39`, `teams.tsx:21`, `team.tsx:77`

**Interfaces:**
- Produces: `yearFromPath(path: string): number | null`; `resolveHistEpoch(manifest, logicalPath): number` (exported for the test); `isLiveYear(year: number): Promise<boolean>` — `year === CURR_YEAR || year === (await getManifest())?.trailing_year`.
- `query()` callers that pass `year === CURR_YEAR` for `checkBucket`/TTL compute `const live = await isLiveYear(year)` first. `team.tsx`'s reconstruct path (`year !== CURR_YEAR`) stays current-year-only (its `team_to_events` blob is current-year data).

- [ ] **Step 1: Failing test** `frontend/tests/yearFromPath.test.ts` — reads `backend/tests/data/year_from_path_vectors.json`, asserts each vector; asserts `resolveHistEpoch` for: manifest without `hist_epochs` → `hist_epoch` (Review Focus 5); `hist_epochs {"2026":3}`, global 2 → 3 for `event/2026mmr`, 2 for `event/2025cc`; global 4 beats 3.
- [ ] **Step 2: Run** `node --test frontend/tests/yearFromPath.test.ts` → FAIL.
- [ ] **Step 3: Implement** (`resolveHistEpoch` lives in `yearFromPath.ts` so the test imports no Next code).
- [ ] **Step 4: Run** the node test → PASS; `cd frontend && yarn build` → "Compiled successfully".
- [ ] **Step 5: Commit** `feat(frontend): live trailing year and per-year hist epochs`.

### Task 9: Docs, operator surface, spec amendment

**Files:**
- Modify: `docs/superpowers/rig/DATA-REFRESH.md` §1, §3–§7; `docs/superpowers/rig/deploy/DEPLOY.md` §3; `backend/CLAUDE.md` (hist-blob gotcha → year epochs; Season Prep drops `make reprocess-year YEAR=<old year>`); `backend/src/constants.py` HIST_EPOCH comment; docstrings `storage.py` `write_hist_blobs`, `main.py` past-year branch; `docs/superpowers/deliverables/historical-backfill.md:83-90`; spec §7 step 1 deviation note.
- Modify: `docs/superpowers/rig/deploy/Makefile` — add `verify-trailing` (Usage: `make verify-trailing [YEAR=2026] [EVENT=2026mmr]`): runs `python3 $(DEPLOY_DIR)/../verify/trailing.py --bucket-url https://$(BLOB_DOMAIN) --api-url $(API_URL) --year $(YEAR) --event $(EVENT)`.
- Create: `docs/superpowers/rig/verify/trailing.py` — prints manifest `trailing_year`, `hist_epochs`, count of the year's non-Parquet year keys; resolves `events/Y` and `event/EVENT` exactly as the frontend does (manifest entry, else per-year hist epoch) and prints status / num_teams / match count of each, plus the API's view of the event; exits non-zero if the resolved event page and the API disagree on `num_teams` or match count.

  Optional `EXPECT_TEAMS=` / `EXPECT_MATCHES=` pass `--expect-teams` / `--expect-matches`, which also fail the run when the resolved page differs.
- [ ] **Step 1:** Write `trailing.py` and the target; run `make verify-trailing YEAR=2026 EVENT=2026mmr EXPECT_TEAMS=32 EXPECT_MATCHES=40` against production now → exits non-zero (resolved page 0 teams). Record that pre-deploy output in the PR.
- [ ] **Step 2:** Doc sweep per Files; the term audit (spec §3 names, no "in-window", no "page blobs").
- [ ] **Step 3: Commit** `docs(trailing): refresh, epochs, rollover runbook; verify-trailing target`.

## Production verification

Run in order after merge to `cph-staging`; record each output in the PR.

1. `cd backend && $PY -m pytest tests -q` → all pass; `node --test frontend/tests/*.test.ts` → pass; `cd frontend && yarn build` → "Compiled successfully".
2. `make ship` (from `docs/superpowers/rig/deploy`) → both builds SUCCESS, both services 100% on the new revision.
3. `make pause-cron`; `make info` → `TRAILING_YEAR: 2026`, `TRAILING_WINDOW_OPEN: true`, `HIST_EPOCHS: {}`.
4. `make update-curr-year` → response `"trailing": "full"`; `MIN=30 make pipeline-logs` shows `2026 TBA`, `2026 EPA`, the trailing publish and `2026` snapshot lines with no traceback; record the full trailing cycle's wall time (decides whether window close fits under gunicorn's 1200 s, spec §7).
5. `make verify-trailing YEAR=2026 EVENT=2026mmr EXPECT_TEAMS=32 EXPECT_MATCHES=40` → `trailing_year 2026`; resolved `event/2026mmr` 32 teams, 40 matches; `events/2026` lists `2026mmr`; exit 0. Repeat for `EVENT=2026mttd` and `EVENT=2026nhalt` → Completed with matches.
6. `make verify-event EVENT=2026mmr` → API num_teams 32, matches count 40.
7. Browser: `playwright-cli` opens `https://statbotics.popcornpenguins.com/event/2026mmr` → page shows 32 teams and match results; `/events?year=2026` (or the year's event list page) includes Minne Mini Regional; a team page of an attendee (`/team/<num>/2026`) lists the event. Screenshot each.
8. `make update-curr-year` again → `"trailing": null` (idle, no change on TBA).
9. `make resume-cron`; `make smoke`; `make verify-web` → 200s and the legacy 301.
10. Later, recorded as follow-ups in the PR: Sun 12-06 – Tue 12-08 `make verify-trailing EVENT=2026isios` while it runs; after Mon 12-28 08:00 UTC `make verify-trailing YEAR=2026 EVENT=2026mmr` → `trailing_year null`, `hist_epochs {"2026": 3}`, 0 non-Parquet 2026 year keys, page resolved from `hist/3/`.
