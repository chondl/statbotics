# Track 1 — EPA consistency — implementation plan

Branch: `epa-consistency` (worktree `.worktrees/epa-consistency`, cut from `a2cea55`).
Scope: spec §2.2 items A–D in one PR. Acceptance: §2.4.

## Verified ingestion / write facts (re-checked at a2cea55)

- **Completion status** is assigned in `src/tba/read_tba.py:get_event_matches` (~line 209): a match is
  `COMPLETED` iff `red_score >= 0 and blue_score >= 0`. Breakdown parsed at ~line 229 via
  `clean_breakdown`; `clean_breakdown` returns `empty_breakdown` (all None/0) when
  `breakdown is None or score is None or score == 0 or year < 2016` (`src/tba/breakdown.py:830`).
  → A score-before-breakdown match is ingested `COMPLETED` with zeroed components.
- **EPA processing** iterates *all* matches (`src/data/epa/calc.py:34`), but `process_match`
  (`src/models/template.py:72`) short-circuits after prediction when `status == UPCOMING`
  (no `attribute_match`/`update_team`). → Demoting a deferred match to `UPCOMING` naturally
  excludes it from EPA updates while still publishing predictions.
- **DB write gate**: `changed()` in `src/data/utils.py:57` filters by `str(obj) != str(prev)`.
  Model `__str__` methods are lossy (omit rank/percentile/norm_epa/component EPAs). Objects are
  `attrs` classes (`generate_attr_class`); `write_all` already does `attr.asdict(x)`
  (`src/db/write/template.py:67`) and runs one transaction/cycle.
- **Blob gate**: `src/google/storage.py:80` gates `event/{key}` on `str(e) != str(orig)`.
  `Event.__str__` omits EPA → event page freezes. `_read_event` (`src/site/event.py:84`) is a pure
  in-memory shaper (no DB/await) → safe to render for a content diff.

## Design decisions

### A. Content-based blob publish gate — **Design 1 (render-diff)**
Render `_read_event(...)` for both the cycle-start DB state (`orig_objs`) and the new state (`objs`);
upload `event/{key}` when the rendered payloads differ (dict inequality). Rationale: smallest diff,
no new per-cycle GCS read/write, and it composes with item C (honest DB diff keeps DB == rendered
truth). Cost: needs a one-time `partial=False` run on deploy to resync historically-stale blobs
(documented in PR). Persisted-hash self-healing (Design 2) is deferred to Track 2, whose manifest
mechanism generalizes this gate (spec §3.5).

### B. Score-before-breakdown deferral
In `get_event_matches`, for `year >= 2016`, when a would-be-`COMPLETED` match has no red/blue score
breakdown, demote status to `UPCOMING` (predictions still publish; EPA replay skips it) **unless** the
match is older than a grace window (fallback). Fallback = age threshold (`match.time` older than
`SCORE_BREAKDOWN_GRACE_HOURS = 24`): recent matches (the race window) defer; old matches — including
events that never post breakdowns — process anyway. Extract the decision into a pure, import-light
helper `src/tba/breakdown_gate.py:should_defer_for_breakdown(...)` for unit testing.

### C. Honest DB write diff
Replace the lossy `str()` compare in `changed()` with full attrs content equality via a pure helper
`src/db/diff.py:content_changed(curr, prev)` (`attr.asdict` compare). `__str__` stays for logging only.

### D. Write efficiency — measurement only
`write_all` already batches to one transaction/cycle (upstream). Measure a partial cycle before/after C
on the rig using the existing `Timer`, and count extra rows from `changed()`. No speculative optimization.

## Tests (spec §4) — dependency-light pytest in `backend/tests/`
- `test_breakdown_gate.py` — deferral policy truth table (stdlib + enums only).
- `test_db_diff.py` — `content_changed` with tiny `attr.s` classes (attrs only).
Add `pytest` to `[tool.poetry.group.dev.dependencies]`. Run via a local venv (pytest+attrs).

## Rig-dependent (pending `RIG.md: STATUS: READY`)
Acceptance 2.4.1, .2, .3, .4, .5, .6 need the end-to-end rig. Unit tests + code land first; rig items
documented in track1.md if the rig is not ready.
