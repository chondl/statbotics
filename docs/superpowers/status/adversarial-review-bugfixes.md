# Adversarial review — bug-fix PRs (#1, #5, #6, #8, tests #3/#4)

Reviewer: adversarial pass, read-only. Goal: NEW problems not already in the PR
bodies / status docs. Rig untouched (only read-only python snippets + the rig
venv for an attrs equality probe; no DB/GCS writes).

Severity: P0 wrong/dangerous · P1 real bug / upstream-blocker · P2 weakness worth
fixing · P3 nit.

---

## NEW findings

### F1 — P2 — Active-season blob write-amplification: `year_changed` republishes ALL event blobs every cycle (undisclosed cost; defeats edge-cacheability)
`src/google/storage.py:92-100` + `src/site/event.py` (`_read_event` embeds
`"year": year_obj.to_dict()`).

The new gate: `new_events = [e for e in events if year_changed or e != orig... or
matches-changed or team_events-changed]`, where
`year_changed = orig_objs is None or year_obj != orig_objs[0]`.
Because the event blob embeds the **entire year object** (`year_obj.to_dict()`),
`year_changed` being True is content-correct — but the year object churns on
essentially every active cycle (year.count, epa_conf/acc/mse, score means,
epa_99p/90p/75p/25p percentile values all move whenever any match completes or any
EPA drifts anywhere in the season). So during a competition weekend the gate
re-uploads **all ~215 event blobs every partial cycle** (vs a handful under
baseline `str(event)` gating). The careful per-event `event_to_matches` /
`event_to_team_events` comparison the PR added is dead code whenever `year_changed`
is True (short-circuit `or`), i.e. most active cycles — the gate effectively
reverts to "republish everything".

Why it matters: (a) the PR body's measured-cost table only reports
"event blobs/cycle (no change) = 0" and never the active-season case, so the cost
claim is **incomplete/misleading**; (b) every event blob getting a fresh
content-hash every cycle churns the CDN/edge cache that the sibling bucket-first
work (PR #2) is built to exploit — event blobs are never warm during live events,
exactly when traffic peaks. Not "wrong" (content is correct) but a material,
undocumented regression an upstream maintainer would question.
NEW.

### F2 — P2 — `str()`→attrs `!=` switch flips NaN semantics: a NaN float field now rewrites its row (and republishes its event blob) every cycle, forever
`src/data/utils.py:58` (`changed()`), `src/google/storage.py:96-99`.

Verified empirically (rig venv, attrs 23.2.0): the generated attr class uses a
field-wise `__eq__` (so the fix genuinely detects rank/norm/component drift — good),
and `C(b=nan) != C(b=nan)` is **True**. The OLD gate compared `str(obj)`, and
`str(nan) == 'nan'`, so a NaN-bearing row stringified equal → treated as UNCHANGED
(no write). The NEW gate treats it as ALWAYS changed → the row is upserted every
cycle and (via the same `!=` on match/team_event lists) its event blob is
re-uploaded every cycle. This silently defeats the PR's core invariant
("steady-state writes converge to ~0").

Latent, not yet firing: fields passed through `r()`
(`int(x*10**n+0.5)/10**n`) would **crash** on NaN before persisting, which guards
norm_epa/percentile/metric fields. But raw `epa` / component `*_epa` and the
`year.epa_{99,90,75,25}p` fields (which store raw epa values with **no** `r()`,
`agg.py:140-154`) are unguarded — if the SkewNormal model ever emits NaN (the
codebase demonstrably produces stats NaNs elsewhere: PR #8's SOS bug is exactly a
NaN-from-degenerate-variance), those rows churn silently. Untested (see F6).
NEW.

### F3 — P2 — Blob gate is not self-healing against a failed/partial upload, and upload exceptions are silently swallowed
`src/data/main.py:77-81` (DB write precedes blob upload),
`src/google/storage.py:42-44` (`upload_files_to_gcs` = `executor.map`, result never
consumed) and `:34-39` (no try/except).

The gate compares in-memory objs against `orig_objs` = a deepcopy of the
**cycle-start DB** state, never against actual blob state. DB write (line 77)
commits before blob uploads (line 81). If a cycle crashes between the two, or a GCS
PUT fails, the DB is advanced but the blob is stale; next cycle `orig == objs` for
any event whose content is now stable → the gate skips it → the stale blob persists
indefinitely with no retry. Worse, event-blob uploads go through
`with ThreadPoolExecutor() as ex: ex.map(upload_file_to_gcs, ...)` whose iterator is
never consumed, so any per-blob upload exception is **silently discarded** (no log,
no raise). This is the exact staleness class PR #1 fixes, recurring through a
different trigger, and is distinct from the acknowledged one-time post-deploy
resync. Mostly a stable/completed-event window (active events self-heal on the next
content change), but the silent-swallow makes any upload failure invisible.
NEW (the one-time deploy resync is KNOWN; this failure-mode is not).

### F4 — P3 — `CUTOFF=250` sized on the average row, not the worst case; a dense batch can still exceed 16 MiB
`src/db/write/template.py:18`.

PR justifies 250 as "~4 MiB average" (15 KB avg team_years row). But the stated max
row is 69 KB; 250 × 69 KB ≈ 17.25 MB > 16 MiB. Chunking is by arbitrary dict order,
so a chunk landing ~243+ near-max rows (plausible late season: top teams with many
per-match JSON entries) can still trip `ProtocolViolation`. The fix lowers the
probability but doesn't remove it; byte-budgeted batching would. NEW.

### F5 — P3 — Deferred match keeps a real final score while marked Upcoming
`src/tba/read_tba.py:279-280`.

`defer_missing_breakdown` flips `status→UPCOMING` and `winner=None` but leaves
`red_score`/`blue_score` populated (and breakdowns are `clean_breakdown`'s imputed
zeros). All backend consumers filter on `status == COMPLETED` (`avg.py:11`,
`wins.py:34`, `metrics.py:136`, `template.py:72`), so aggregates are correctly
unaffected — good. But the published match/event blobs then carry an "Upcoming"
match with a real 161-64-type score; the frontend match/event views may render a
score on a match flagged upcoming for up to 24 h. Cosmetic, transient. NEW.

### F6 — P3 — Test gap (#4): NaN stability and the storage-gate list comparison are untested
`backend/tests/test_write_gate.py`.

Tests cover rank/norm/component drift and `ty != None` (good, and they assert
`str(a)==str(b)` to prove the old gate missed the drift — nice). But nothing tests a
NaN-bearing row (would fail `assert ty == ty`, exposing F2), and nothing exercises
the actual call sites — `changed()` in utils.py or the ordered-list comparison
`event_to_matches[e.key] != orig_matches[e.key]` in storage.py (whose correctness
depends on curr/orig match dicts iterating in the same order; currently safe only
because `orig_objs` is a deepcopy of the initial objs, an untested invariant).
Deferral tests of the pure predicate are solid. NEW.

### F7 — P3 (low-confidence) — dedup map is a module singleton (SSR cross-request sharing if ever server-run)
`frontend/src/api/storage.tsx:40`.

`const inFlight = {}` is module-level. Rejection/cleanup are handled correctly
(`.finally(() => delete ...)` clears on both resolve and reject; no rejection
caching; no unbounded growth). The only latent risk: if `query()` ever executes
during Next.js server rendering, the map is shared across all users' requests. In
practice `query()` depends on IndexedDB (`idb-keyval`, browser-only), so it is
effectively client-only and the risk is theoretical. Noted for completeness. NEW.

---

## KNOWN items confirmed (not re-reported as discoveries)
- One-time post-deploy blob resync required (PR #1 deploy note) — confirmed the gate
  compares cycle-start DB, cannot retroactively heal. (F3 is a *different* failure
  mode.)
- Residual `team_to_events` double-fetch via Track 2 `fetchBucketData()` — confirmed
  out of scope for the upstream `query()` dedup.
- Worker still swallows future rejections; stale `APITeamEvent.epa.total_points` type
  — confirmed present, documented as noted-not-fixed in sos-sim-fix.md.
- 7px mobile overflow / legacy `?t=` cache-buster (`storage.tsx:56`) — accepted P3s.
- `/docs/rest` prod-hardcode — staging-only, out of scope.

## Checks that came back CLEAN (ruled-out attack angles)
- attrs `__eq__` is field-wise (overrides `Model.__eq__` pk-only) — fix is real.
- numpy value leak: `C(b=np.float64(2.0)) == C(b=2.0)` is True → equal numerics are
  stable; only NaN (F2) breaks.
- `r()`-wrapped fields cannot silently persist NaN (they crash first).
- Deferral tz: uses `datetime.now().timestamp()` (correct UTC epoch), not `utcnow()`.
- Deferral downstream: avg/wins/metrics/model all gate on `status==COMPLETED`.
- PR #8 `|| 1e-9` precedence correct (`/` binds tighter than `||`); variance floor
  1e-9 (sd≈3e-5) >> float residual (~1e-13) so degenerate percentile stays ≈0.5;
  N=0 paths yield 0.5 or a finite Gaussian, no crash.
- PR #6 `.nullslast()` valid on both CRDB and Postgres; `greatest()` ignores nulls
  (null only when both alliances null) — matches PR reasoning.
- PR #5 useMemo/`data?.match?.key == match_id` — loose `==` matches existing event-
  page pattern; the `[...blue.team_keys]` spread-if-undefined is pre-existing, not a
  regression.
- CUTOFF is pure batch chunking; no transaction-size assumption elsewhere.
