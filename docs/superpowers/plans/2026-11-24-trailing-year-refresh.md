# Trailing-Year Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the trailing year's events refreshing and publishing like the
current year until Dec 28 00:00 PT, and give past years per-year hist epochs so
their pages can never stay stale.

**Architecture:** One module owns the trailing-window rule and the live-year
predicate. The manifest gains `trailing_year` and `hist_epochs`, and every write
goes through one read-mutate-conditional-write helper. Trailing cycles publish
versioned blobs through the manifest; past-year reprocesses write hist blobs
under a year epoch that advances on any change; window close turns the
trailing year into an ordinary past year. Heavy trailing work runs in plain
`def` endpoints (off the event loop) under one process-wide pipeline lock.

**Tech Stack:** Python 3.11, FastAPI, google-cloud-storage 3.1.0, pytest;
Next.js 13 frontend (TypeScript 4.9), `node --test` on Node 24.

**Spec:** [2026-11-24-trailing-year-refresh-design.md](../specs/2026-11-24-trailing-year-refresh-design.md)

## Global Constraints

- Trailing year = `CURR_YEAR − 1`. Trailing window open ⇔ now < Dec 28 00:00 `America/Los_Angeles` of the trailing year (2026-12-28 08:00 UTC for 2026).
- Live year = `CURR_YEAR`, or the trailing year while the trailing window is open.
- No hist blob is ever written for a live year (enforced in `publish_past_year`).
- Full trailing cycles and window close fetch TBA fresh: `process_year(..., cache=False, ...)`.
- Every `manifest.json` write goes through `update_manifest`: read pinned to one generation, `mutate` idempotent, write with `if_generation_match`; on `PreconditionFailed` re-read, re-apply, retry up to 5 times, then raise.
- `read_manifest` returns `None` only on NotFound; any other failure raises `ManifestReadError`, and writers abort.
- Hist resolution: a manifest entry wins; else `hist/{max(hist_epochs[year] ?? 0, hist_epoch)}/{path}`. A year-epoch advance allocates `e + 1` where `e` is that effective epoch. A global `HIST_EPOCH` bump must exceed `max(hist_epochs)`; `/info` reports `HIST_EPOCH_SHADOWED: true` when it does not.
- Year-from-path: `event/{key}` → first 4 digits of key; `events/Y…`, `team_years/Y…`, `noteworthy_matches/Y…` → `Y`; `parquet/Y/…` → `Y`; `team/{num}/Y` (Y four digits) → `Y`; anything else → no year.
- Site-blob renderers keep today's input order (no sorting), so renders match the existing `hist/2` bytes and no year advances without a data change.
- Pipeline lock: `src.data.pipeline_lock.PIPELINE_LOCK` (`threading.Lock`). Trailing endpoints acquire non-blocking and return `{"status": "busy"}` if held; the current-year cycle, the sweep and `refresh_teams` acquire non-blocking and skip with a log line if held.
- `tzdata` is added to `backend/requirements.txt`.
- Exact completion log lines (stdout): `TRAILING {year} CYCLE COMPLETE full={0|1} secs={n}`, `TRAILING {year} CLOSE {outcome} secs={n}`, `YEAR EPOCH {year} {unchanged|gap_filled|advanced} epoch={e}`.
- Tests: `PY=/tmp/claude-1000/-repos-popcornpenguins-statbotics/a31463e1-e814-4c6a-8829-d9fb2483bbd1/scratchpad/venv/bin/python`, run `$PY -m pytest tests -q` from `backend/` (baseline 199 passed). Frontend: `node --test frontend/tests/` and `yarn build` from `frontend/`.
- Terms exactly as spec §3. Also: **gate** = `GET /v3/site/update_curr_year`; **bootstrap** = the first full trailing cycle of a rollover.

**Deviation from spec §7 step 1 (recorded; spec amended in Task 9):** the final
full trailing cycle at window close runs with **past-year** semantics. After
close the year is an ordinary past year; computing it the way the sweep will
later compute it avoids a spurious year-epoch advance on the next sweep visit.
The two semantics differ: an event that never posted a match becomes Invalid
(dropped from `events/Y`, no team events), and an Ongoing event with no elims
becomes Completed. Both are upstream past-year behaviour, tested in Task 6.

## Review Focus

1. **Manifest read failure mid-cycle** — must abort the publish, never plan from empty (would erase `hist_epochs`). Task 2.
2. **Two writers racing** — a current-year publish and a trailing publish interleaving must both land. Task 2.
3. **Cold year** — `reset_all_years` on an empty bucket gap-fills under the global epoch, no advance; `write_parquet` creates a missing manifest. Task 5.
4. **Close instant** — 2026-12-28 07:59:59 UTC is open, 08:00:00 is closed; a trailing publish that commits after close aborts. Tasks 1, 6.
5. **Frontend with an old manifest** (no `hist_epochs`, no `trailing_year`) — resolves exactly as today. Task 8.

---

### Task 1: Trailing-window rule, live-year predicate, pipeline lock

**Files:**
- Create: `backend/src/data/trailing.py`, `backend/src/data/pipeline_lock.py`
- Modify: `backend/tests/conftest.py` (autouse fixture), `backend/requirements.txt` (`tzdata`)
- Test: `backend/tests/test_trailing_window.py`

**Interfaces:**
- Produces: `trailing_year() -> int`; `trailing_window_open(now: Optional[datetime] = None) -> bool` (aware UTC; default now); `is_live_year(year: int, now: Optional[datetime] = None) -> bool`; `PIPELINE_LOCK: threading.Lock`.
- conftest autouse fixture `_trailing_window_closed` monkeypatches `src.data.trailing.trailing_window_open` to `lambda now=None: False`; tests that need it open request fixture `trailing_open` (also in conftest). Every later task's tests rely on this.
- A comment block at the top of `trailing.py` classifies every `CURR_YEAR` use in `backend/src` (from `grep -rn CURR_YEAR backend/src`): live-year — `tba.py:92`, `tba.py:303`, `router.py:136`; everything else current-year-only, listed by file:line.

- [ ] **Step 1: Failing tests** — `test_open_before_close` (2026-12-28T07:59:59Z → True), `test_closed_at_close` (08:00:00Z → False), `test_open_at_rollover` (2026-11-06T07:00Z → True), `test_live_years` (CURR_YEAR always live; CURR_YEAR−1 live only while open; CURR_YEAR−2 never). These tests call the pure `trailing._window_open_at(now)` and `trailing._is_live_year_at(year, now)`, which the conftest fixture does not patch.
- [ ] **Step 2: Run** `$PY -m pytest tests/test_trailing_window.py -q` → FAIL.
- [ ] **Step 3: Implement** pure `_window_open_at(now: datetime) -> bool` and `_is_live_year_at(year, now) -> bool`; `trailing_window_open(now=None)` and `is_live_year(year, now=None)` delegate to them (with `now` defaulting to `datetime.now(timezone.utc)`), and `is_live_year` calls `trailing_window_open` so the fixture governs it; `ZoneInfo("America/Los_Angeles")` resolved at import.
- [ ] **Step 4: Run full suite** → 199 + new pass.
- [ ] **Step 5: Commit** `feat(trailing): trailing-window rule, live-year predicate, pipeline lock`.

### Task 2: Manifest model and integrity

**Files:**
- Modify: `backend/src/google/publish.py`; `backend/src/google/storage.py` (`read_manifest`, `write_manifest`, `_publish`, `write_objs:317`, `publish_team_artifacts:334-374`, `gc_versioned_blobs:381`); `backend/src/google/parquet.py:65-90`; `backend/src/models/backtest.py:136` (caller of `read_manifest`)
- Create: `backend/src/google/year_path.py`, `backend/tests/data/year_from_path_vectors.json`
- Modify: `backend/tests/test_hist_blobs.py` `FakeBucket` → extend into `backend/tests/fake_gcs.py` (one shared fake; `test_snapshot_format.py` switches to it)
- Test: `backend/tests/test_manifest_integrity.py`, `backend/tests/test_year_path.py`, `backend/tests/test_team_blob_gate.py` (rewritten for the new `UploadPlan`, same assertions)

**Interfaces:**
- `Manifest`: new fields `trailing_year: Optional[int] = None`, `hist_epochs: Dict[str, int]`, plus `extra: Dict[str, Any]` holding any unknown top-level keys; `to_json`/`from_json` round-trip all of them.
- `UploadPlan(uploads, legacy_uploads, blob_updates: Dict[str, str], blob_removals: Set[str])`; `plan_uploads(rendered, prev, cycle)` fills `blob_updates` only for changed digests.
- `year_from_path(path: str) -> Optional[int]`; `effective_hist_epoch(manifest: Manifest, year: Optional[int]) -> int`.
- `class ManifestReadError(Exception)`; `read_manifest() -> Optional[Manifest]`.
- `update_manifest(mutate: Callable[[Manifest], None], bucket=None) -> Manifest`: `blob = bucket.get_blob(MANIFEST_OBJECT)`; if None → start from `Manifest()` with precondition 0; else download `bucket.blob(MANIFEST_OBJECT, generation=blob.generation)` (NotFound on that pinned read → retry). Apply `mutate` to the parsed copy, stamp `cycle` and `hist_epoch = max(prev.hist_epoch, HIST_EPOCH)`, drop `hist_epochs` entries `<= hist_epoch`, write gzip JSON with `if_generation_match`. Retry ≤5 on `PreconditionFailed`, then raise `ManifestConflictError` (module counter `manifest_conflicts` surfaced in `/info`).
- `_publish`, `publish_team_artifacts`, `write_parquet` commit via `update_manifest` applying `blob_updates`/`blob_removals`. `write_parquet` creates the manifest when absent (Review Focus 3). `gc_versioned_blobs` refuses (returns `{"status": "refused"}`) on `None` or error.
- Vectors JSON entries `{"path", "year"}`: `event/2026mmr`→2026, `events/2026`→2026, `team_years/2026`→2026, `team_years/2026.limit=100.metric=epa`→2026, `noteworthy_matches/2026`→2026, `team/6238/2026`→2026, `team/6238/years`→null, `team/6238`→null, `teams/all`→null, `events/all`→null, `team_to_events`→null, `upcoming_matches.limit=20.metric=max_epa`→null, `parquet/2026/team_years.parquet`→2026.

- [ ] **Step 1: Failing tests**
  - `test_year_from_path_vectors`.
  - `test_manifest_round_trips_new_and_unknown_fields`.
  - `test_update_manifest_preserves_trailing_state`.
  - `test_update_manifest_retries_on_412` (a concurrent writer's key and ours both present at the end).
  - `test_update_manifest_reads_pinned_generation` (content changes between `get_blob` and download → pinned read sees the old generation, write gets 412, retry wins).
  - `test_update_manifest_exhaustion_raises_and_counts`.
  - `test_read_error_aborts_write` (non-NotFound download error → `ManifestReadError`, no upload).
  - `test_effective_epoch_max_rule`, `test_epochs_at_or_below_global_dropped`.
  - `test_gc_refuses_without_manifest`.
  - `test_write_parquet_creates_missing_manifest`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** → pass.
- [ ] **Step 5: Commit** `feat(manifest): trailing/epoch fields, pinned conditional writes, fail-closed reads`.

### Task 3: Live-year semantics in the pipeline and ping

**Files:**
- Modify: `backend/src/data/tba.py:92` (`get_event_status`), `:303` (`full_curr_year` → `full_live_year`); `backend/src/data/router.py:136`
- Test: `backend/tests/test_live_year_status.py`, `backend/tests/test_ping.py`

**Interfaces:** consumes `is_live_year`, `trailing_year` (Task 1).

- [ ] **Step 1: Failing tests** (`trailing_open` fixture where needed):
  - `test_trailing_event_without_matches_is_upcoming`, `test_trailing_event_with_quals_only_is_ongoing`, `test_past_year_event_without_matches_is_invalid` (window closed).
  - `test_check_year_partial_probes_ongoing_trailing_event` (an Ongoing trailing event's `/matches` etag change returns True).
  - `test_ping_accepts_trailing_key_while_open` (202), `test_ping_rejects_trailing_key_after_close` (204).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement**: `get_event_status` tests `is_live_year(year)`; `full_live_year = not partial and is_live_year(year_num)`; ping accepts `{CURR_YEAR}…` always and `{trailing_year()}…` while the window is open.
- [ ] **Step 4: Run full suite** → pass (`test_offseason_stale_probes.py` uses 2026 and runs under the closed-window default).
- [ ] **Step 5: Commit** `feat(trailing): live-year event statuses and ping keys`.

### Task 4: Render-only site blobs

**Files:**
- Modify: `backend/src/google/storage.py:425-515` — replace `write_hist_blobs` and `upload_historical` with `render_site_blobs`
- Test: `backend/tests/test_hist_blobs.py`

**Interfaces:**
- Produces: `render_site_blobs(year: int, objs: objs_type, teams: List[Team]) -> Dict[str, bytes]` — `compress`ed payloads for the four site-blob families, inputs in today's order (no sorting). `write_hist_blobs` and `upload_historical` are deleted; callers move to Task 5's `publish_past_year`.

- [ ] **Step 1: Failing test** `test_render_matches_previous_hist_bytes` (fixture objs; bytes per path equal what the old `write_hist_blobs` uploaded, captured before deleting it — commit the expected digests as a fixture). Remove the skip-existing tests at `test_hist_blobs.py:206-217` (their behaviour moves to Task 5).
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4:** Tasks 4 and 5 are implemented by one subagent and committed together, because `process_year` loses `write_hist_blobs` here and gains `publish_past_year` in Task 5; the full suite must pass at the end of Task 5.

### Task 5: Year epochs for past years

**Files:**
- Create: `backend/src/google/year_epoch.py`
- Modify: `backend/src/data/main.py:150-163` (past-year branch of `process_year`), `reprocess_year:392`
- Test: `backend/tests/test_year_epoch.py`

**Interfaces:**
- Consumes: `render_site_blobs` (Task 4); `update_manifest`, `effective_hist_epoch`, `year_from_path`, `fake_gcs` (Task 2); `is_live_year` (Task 1).
- Produces: `publish_past_year(year: int, rendered: Dict[str, bytes], bucket=None) -> str` → `"unchanged" | "gap_filled" | "advanced"`. Raises `ValueError("live year")` for a live year.
  - `e = effective_hist_epoch(read manifest, year)`; per path `bucket.get_blob(f"hist/{e}/{path}")` (thread pool), compare `md5_hash` with base64 md5 of the rendered bytes.
  - All missing → upload under `e` with `IMMUTABLE_CACHE` → `"gap_filled"`.
  - Any differs or any missing → upload all under `e + 1`, overwriting, then `hist_epochs[str(year)] = e + 1` → `"advanced"`.
  - Else `"unchanged"`.
  - In every outcome, the year's non-Parquet year keys present in `blobs` are removed in the same `update_manifest` call (skipped when there is nothing to change).
  - Prints `YEAR EPOCH {year} {outcome} epoch={e or e+1}`.
- `process_year` past-year branch: `write_parquet`, then `publish_past_year(year, render_site_blobs(...))`; never reached for a live year (Task 6 adds that branch).
- `reprocess_year(year)` for a live year delegates to `update_trailing_year(full=True)` (Task 6) instead of the past-year path.

- [ ] **Step 1: Failing tests** — `test_unchanged_year_writes_nothing`; `test_changed_path_advances_whole_year` (uploads before manifest); `test_cold_year_gap_fills_under_global_epoch`; `test_removes_shadowing_year_keys_but_keeps_parquet`; `test_advance_rerunnable_after_crash`; `test_refuses_live_year`.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** → pass.
- [ ] **Step 5: Commit** `feat(hist): per-year epochs; past-year reprocesses can never leave pages stale`.

### Task 6: Trailing cycle, trailing publish, window close

**Files:**
- Create: `backend/src/google/trailing_publish.py`
- Modify: `backend/src/data/main.py` (new `update_trailing_year`, `close_trailing_year`; `process_year` branch for a live non-current year); `backend/src/data/sweep.py:36-49`
- Test: `backend/tests/test_trailing_cycle.py`, `backend/tests/test_tba_sweep.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 4, 5.
- `trailing_publish(year, objs, teams) -> bool`: renders site blobs + `build_parquet_uploads(year, …)`, uploads changed versioned objects, then one `update_manifest` whose `mutate` first raises `TrailingClosed` if `not is_live_year(year)` (Review Focus 4), then applies `blob_updates`. Returns whether `parquet/{year}/team_years.parquet` changed.
- `stamp_trailing_bootstrap(year)`: one `update_manifest` that removes the year's year keys not rendered by the last trailing publish (Parquet keys kept) and sets `trailing_year = year`. Called only after `write_snapshot` on a full cycle.
- `update_trailing_year(full: bool) -> Dict`: holds `PIPELINE_LOCK` (non-blocking; `{"status": "busy"}`); partial reads `read_snapshot(year)` and falls back to full if unreadable; full uses `load_teams_tba(cache=False)` and `create_objs(year)`; runs `process_year(year, partial, partial, cache=False if full else True, teams, objs, None)`. `process_year`'s live-non-current branch calls `trailing_publish`, then `write_snapshot(year, …)`; then on full `stamp_trailing_bootstrap`. Writes `trailing/status.json` (`{"year", "state": "running"|"ok"|"error", "full", "started", "finished", "error"}`) at start and end. When `trailing_publish` returned True, schedule a background self-call `GET /v3/data/reset_curr_year` (the chain runs as its own request). Prints `TRAILING … CYCLE COMPLETE`.
- `close_trailing_year() -> Dict`: holds `PIPELINE_LOCK`; `"not_needed"` unless the window is closed and (`manifest.trailing_year` set or non-Parquet year keys of the year remain); else full `process_year(year, False, False, False, …)` with past-year semantics (Task 5 branch) → `publish_past_year`; then `update_manifest` sets `trailing_year = None`; chain `reset_curr_year` self-call if the team_years Parquet changed; writes `trailing/status.json`; prints `TRAILING … CLOSE closed`.
- `normalize_cursor` advances past any live year.
- `_update_curr_year`, `revalidate_tba`, `refresh_teams` try-acquire `PIPELINE_LOCK`; on failure print `PIPELINE BUSY: skipped <name>` and return.

- [ ] **Step 1: Failing tests** (fake GCS; `process_year_tba` monkeypatched to fixture objs):
  - `test_full_trailing_cycle_fetches_fresh` (asserts `cache=False` reaches `process_year_tba` / `get_tba` makes a request despite cached pickles);
  - `test_bootstrap_publishes_then_snapshot_then_marker` (order of writes);
  - `test_bootstrap_removes_unrendered_year_keys`;
  - `test_trailing_publish_is_hash_gated`;
  - `test_trailing_cycle_writes_no_hist`;
  - `test_chain_scheduled_only_when_team_years_change`;
  - `test_publish_after_close_aborts`;
  - `test_busy_when_locked`;
  - `test_close_not_needed_while_open`;
  - `test_close_retires_year` (year keys gone except Parquet, `trailing_year` None, `hist_epochs` advanced when hist pre-existed);
  - `test_close_drops_matchless_event_and_completes_no_elim_event`;
  - `test_close_rerun_after_crash_at_each_step` (after Parquet, after hist uploads, after manifest write);
  - `test_reprocess_year_of_live_year_delegates`;
  - `test_sweep_skips_live_trailing_year` (with `trailing_open`); existing sweep tests stay unchanged under the closed default.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** → pass.
- [ ] **Step 5: Commit** `feat(trailing): trailing cycles, trailing publish, window close`.

### Task 7: Gate, endpoints, `/info`

**Files:**
- Modify: `backend/src/data/router.py`, `backend/main.py:73`
- Test: `backend/tests/test_trailing_gate.py`, `backend/tests/test_ping.py`

**Interfaces:**
- `trailing_gate_action() -> Optional[str]` → `"close" | "full" | "partial" | None`: `"close"` when close would act; `None` when the window is closed otherwise; `"full"` when `manifest.trailing_year != trailing_year()` or the trailing snapshot is unreadable; `"partial"` when `check_year_partial_tba(year, …)` is True; else None.
- The site gate computes the current-year result as today, then **always** evaluates `trailing_gate_action()` inside `try/except` (an exception logs and yields `None`; the gate never 500s), including on the current-year early-return paths. Each positive result schedules its own background self-call: `/v3/data/update_trailing_year?full=1|0` or `/v3/data/close_trailing_year`. Response adds `"trailing": <action|null>`; current-year `status` values unchanged.
- `GET /v3/data/update_trailing_year?full=` and `GET /v3/data/close_trailing_year` are plain `def` handlers.
- `/info` adds `TRAILING_YEAR`, `TRAILING_WINDOW_OPEN`, `TRAILING_STATUS` (from `trailing/status.json`, best-effort), `HIST_EPOCHS` (best-effort), `HIST_EPOCH_SHADOWED`, `MANIFEST_CONFLICTS`.

- [ ] **Step 1: Failing tests** — `test_gate_bootstraps_when_marker_absent`; `test_gate_partial_on_events_list_etag_change`; `test_gate_partial_on_match_change`; `test_gate_partial_on_dropped_candidate`; `test_gate_idle_when_unchanged`; `test_gate_closes_after_window`; `test_gate_idle_after_close_done`; `test_gate_trailing_runs_when_current_snapshot_unreadable`; `test_gate_survives_manifest_read_error`; existing `test_ping.py` gate tests pass with `"trailing"` added.
- [ ] **Step 2: Run** → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run full suite** → pass.
- [ ] **Step 5: Commit** `feat(trailing): gate probes the trailing year; endpoints and /info`.

### Task 8: Frontend live year and per-year epochs

**Files:**
- Create: `frontend/src/api/yearFromPath.ts` (no imports; erasable TS syntax only), `frontend/tests/yearFromPath.test.mjs`
- Modify: `frontend/src/api/storage.tsx` (Manifest interface, `resolveBucketUrl`, export `isLiveYear`), `frontend/src/pages/event/[event_id].tsx:49`, `frontend/src/api/events.tsx:9`, `matches.tsx:39`, `teams.tsx:21`, `team.tsx:77`

**Interfaces:**
- `yearFromPath(path: string): number | null`; `resolveHistEpoch(manifest: {hist_epoch?: number; hist_epochs?: Record<string, number>}, logicalPath: string): number`.
- `isLiveYear(year: number): Promise<boolean>` — `year === CURR_YEAR || year === (await getManifest())?.trailing_year`. (Spec §5.3's `isLiveYear(year, manifest)` becomes this async form; amended in Task 9.)
- Call sites compute `const live = await isLiveYear(year)` and pass `live` for `checkBucket` and TTL; the event-page ping effect awaits it. `team.tsx`'s reconstruct path stays `year !== CURR_YEAR`.

- [ ] **Step 1: Failing test** `frontend/tests/yearFromPath.test.mjs` imports `../src/api/yearFromPath.ts`, reads `backend/tests/data/year_from_path_vectors.json` via `new URL(..., import.meta.url)`, asserts every vector, and `resolveHistEpoch`: no `hist_epochs` → `hist_epoch`; `{"2026":3}` with global 2 → 3 for `event/2026mmr`, 2 for `event/2025cc`, 2 for `teams/all`; global 4 beats 3.
- [ ] **Step 2: Run** `node --test frontend/tests/` → FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** node test → PASS; `yarn build` (from `frontend/`) → "Compiled successfully".
- [ ] **Step 5: Commit** `feat(frontend): live trailing year and per-year hist epochs`.

### Task 9: Operator surface, docs, spec amendment

**Files:**
- Create: `docs/superpowers/rig/verify/trailing.py` (stdlib only; imports `year_from_path`/`effective_hist_epoch` logic by loading `backend/src/google/year_path.py` with `importlib.util.spec_from_file_location`, so there is no third copy)
- Modify: `docs/superpowers/rig/deploy/Makefile` — targets, each one recipe line with a `##` help comment:
  - `verify-trailing` — `python3 $(MAIN_DIR)/docs/superpowers/rig/verify/trailing.py --bucket-url https://$(BLOB_DOMAIN) --api-url $(API_URL) --year $${YEAR:-2026} --event $${EVENT:-2026mmr} $(if $(EXPECT_TEAMS),--expect-teams $(EXPECT_TEAMS)) $(if $(EXPECT_MATCHES),--expect-matches $(EXPECT_MATCHES))`. No global `YEAR ?=`.
  - `manifest-backup` — copies `gs://$(BUCKET_NAME)/manifest.json` to `gs://$(BUCKET_NAME)/backups/manifest.<UTC timestamp>.json` and prints the source generation.
  - `pipeline-errors` — like `pipeline-logs` but `logName:"stderr"` filtered to `Traceback OR "WORKER TIMEOUT"`.
- `trailing.py` behaviour: fetches `manifest.json?cb=<time>` (Cloudflare MISS); prints `trailing_year`, `hist_epochs`, count of the year's non-Parquet year keys; resolves `events/Y` and `event/EVENT` exactly as the frontend does and prints status, `num_teams`, match count, and whether `EVENT` is in `events/Y`; prints the API's `/v3/event/EVENT` and `/v3/matches?event=EVENT` counts; exits non-zero if the resolved page and the API disagree, if `EVENT` is missing from `events/Y`, or if an `--expect-*` value differs.
- Docs: DATA-REFRESH.md §1, §3–§7 (gate diagram, triggers, the "no lock" caveat replaced by the manifest precondition + pipeline lock); DEPLOY.md §3 and §6 item 3; Makefile comments at lines 210-241 ("hist blobs" wording); backend/CLAUDE.md hist-blob gotcha (year epochs; global bump must exceed `max(hist_epochs)`) and "Data Pipeline" paragraph and Season Prep (drop `make reprocess-year YEAR=<old year>`; note the trailing window and Dec 28); `constants.py` HIST_EPOCH comment; `main.py` past-year branch docstring; historical-backfill.md:83-90; spec §5.3 (`isLiveYear` async), §7 step 1 deviation, §10 new grammars (gate `trailing` field and actions, `publish_past_year` outcomes, `trailing/status.json`, completion log lines).

- [ ] **Step 1:** Write `trailing.py` and the targets; run `make verify-trailing YEAR=2026 EVENT=2026mmr EXPECT_TEAMS=32 EXPECT_MATCHES=40` (from `docs/superpowers/rig/deploy`) against production now → exits non-zero (`2026mmr` not in `events/2026`; resolved page 0 teams). Record the output for the PR.
- [ ] **Step 2:** Doc sweep per Files; term audit (spec §3 names; no "in-window", no "page blobs").
- [ ] **Step 3: Commit** `docs(trailing): refresh, epochs, rollover runbook; verify targets`.

## Production verification

Run in order; record each output in the PR. Rollback before Mon 12-28: revert the merge and `make ship` (old code ignores the new manifest fields; 2026 reverts to today's stale state). Rollback after window close: also restore `hist_epochs` handling, or 2026 falls back to the stale `hist/2/`.

1. Local gate: `$PY -m pytest tests -q` (from `backend/`) → all pass; `node --test frontend/tests/` → pass; `yarn build` (from `frontend/`) → "Compiled successfully".
2. Merge the PR; `git switch cph-staging && git pull --ff-only` in the primary checkout (`check-deploy-src` refuses any other branch).
3. From `docs/superpowers/rig/deploy`: `make pause-cron` **before** shipping, then `make manifest-backup` (record the generation).
4. `make ship` → both builds SUCCESS, both services 100% on the new revision (work the DEPLOY.md §2 checklist as todos).
5. `make info` → `TRAILING_YEAR: 2026`, `TRAILING_WINDOW_OPEN: true`, `HIST_EPOCHS: {}`, `HIST_EPOCH_SHADOWED: false`, `MANIFEST_CONFLICTS: 0`.
6. `make update-curr-year` → JSON with `"trailing": "full"`. Then `MIN=30 make pipeline-logs` until `TRAILING 2026 CYCLE COMPLETE full=1 secs=<n>` appears; record `n` and the chained `reset_curr_year` duration. `MIN=30 make pipeline-errors` → no `Traceback`, no `WORKER TIMEOUT`. `make info` → `TRAILING_STATUS.state: ok`.
7. Wait 2 minutes (manifest edge + in-process caches), then `make verify-trailing YEAR=2026 EVENT=2026mmr EXPECT_TEAMS=32 EXPECT_MATCHES=40` → exit 0, `trailing_year 2026`, `2026mmr` in `events/2026`. Repeat `EVENT=2026mttd` and `EVENT=2026nhalt` → exit 0, status Completed.
8. `make verify-event EVENT=2026mmr` → num_teams 32, matches count 40.
9. Browser, fresh `playwright-cli` context: `https://statbotics.popcornpenguins.com/event/2026mmr` shows 32 teams and match results; the 2026 events page lists Minne Mini Regional; `/team/<attendee>/2026` lists the event. Screenshot each.
10. `make update-curr-year` again → `"trailing": null`.
11. `make resume-cron`; `make smoke`; `make verify-web` → 200s and the legacy 301.
12. Follow-ups recorded in the PR: Sun 12-06 – Tue 12-08 `make verify-trailing EVENT=2026isios` while it runs; after Mon 12-28 08:00 UTC `make verify-trailing YEAR=2026 EVENT=2026mmr` → `trailing_year null`, `hist_epochs {"2026": 3}`, 0 non-Parquet 2026 year keys, `make pipeline-logs` shows `TRAILING 2026 CLOSE closed`.
