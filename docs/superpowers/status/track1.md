# Track 1 status — COMPLETE

All spec §2.2 items implemented, all §2.4 acceptance criteria verified on the rig,
user review feedback applied, branches force-pushed, PR bodies updated.

## Branches / PRs (fork chondl/statbotics only)
- `epa-consistency` (PR #1, draft) — 4 commits on `a2cea55`:
  - bbcc2b1 Compare all fields when deciding whether to write DB rows
  - 06f8614 Reduce upsert batch size to fit CockroachDB message limit
  - 3050e1f Defer EPA processing of matches missing score breakdowns
  - 146b763 Publish event blobs when their content changes
- `epa-consistency-tests` (PR #4, draft, base = epa-consistency): 52d4044 —
  backend/tests/ + pytest dev-dep (12 tests, all pass).
- PR #1 body updated from `track1-pr-description.md` (restructured per feedback:
  one section per change, why-explanations moved from code comments to PR text).

## Review feedback — all applied
1. Tests moved off the PR branch → `epa-consistency-tests`, draft PR #4. ✔
2. breakdown_gate.py removed; deferral inlined in read_tba.py (`defer_missing_breakdown`). ✔
3. db/diff.py removed; write gate is one line in utils.py (`obj != prev.get(obj.pk())`,
   attrs full-field `__eq__` — verified generated eq shadows Model's pk-eq; ~100x faster
   than attr.asdict, 0.1s/cycle for 30K objects). ✔
4. All added comments removed. ✔
5. storage.py diff minimized (~30 lines): constituent-eq gate (year/event/matches/
   team_events), not a render-diff — avoids a false-positive found on the rig
   (CRDB JSONB reorders pre_epas/epas dict keys → rendered list order differs while
   content is equal; render-diff republished all 213 blobs every cycle). ✔
6. PR description restructured. ✔

## Acceptance results (spec §2.4)
1. Repro closed — fixture no-match event: event-blob EPA == team_years-blob EPA (PASS);
   stale-blob A/B: baseline leaves blob frozen at zeros, branch republishes (PASS).
2. #413 closed — 50-row norm_epa perturbation: baseline 50/50 still wrong, branch 0/50 (PASS).
3. Breakdown race closed — TBA-intercept on 2026caasv_qm10: deferred match == no-match
   replay (0/6 teams differ); baseline craters (11096: 26.4→13.6); heal restores exact
   original state, processed once (PASS).
4. No stuck matches — same match >24h old, no breakdown → processed Completed (PASS).
5. Performance quantified — cycle 13.0–13.3s → 13.1–13.2s; Write DB 0.07→0.3–0.6s;
   0 rows/0 event blobs on no-change cycles; one-time 529-row catch-up (PASS).
6. Byte-identical replay — SHA-256 910b184c…7f04 identical baseline vs branch (PASS).

Extra (coordinator-flagged): CRDB 16 MiB limit — measured team_years batch ~15 MiB avg
at CUTOFF=1000 (ProtocolViolation "18 MiB > 16 MiB" reproduced at default settings);
CUTOFF=250 (~4 MiB) succeeds at the 16 MiB default. Included as commit 06f8614,
documented in PR §4.

Smoke suite: 10/10 with branch code serving (incl. --run-update). Rig restored to
rig-local servers afterwards (9/9 without update check).

## Unit tests
12 passed (tests branch): test_write_gate.py (attrs eq catches rank/norm_epa/component
drift the lossy __str__ missed), test_breakdown_deferral.py (policy truth table + 24h
boundary). Run: PYTHONPATH=backend:<pytest-lib> python -m pytest backend/tests -q.
