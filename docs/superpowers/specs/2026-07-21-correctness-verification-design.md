# Correctness Verification Harness: EPA Value Parity + Current-Year Invariants

**Date:** 2026-07-21
**Status:** Proposed (first full run executed 2026-07-21 against the perf deploy — clean, 0 defects)

Perf and storage work must never change a rating. This spec defines a standing,
repeatable harness that proves calculated EPA, percentiles, ranks, and stats are
unchanged across deploys of the mirror (`api-statbotics.iterativerefinement.com`
+ bucket `statbotics-staging-site`). It generalizes the one-off verification run
on 2026-07-21 that validated the db-less flip and four perf features. The
verification is machine-checked end to end — no eyeballed JSON, ever.

Two scripts are the seed, written in the 2026-07-21 session scratchpad and to be
committed under `docs/superpowers/rig/verify/` (same pattern as
[`deploy/`](../rig/deploy/DEPLOY.md) keeping its `Makefile` and `deploy.sh`
beside the doc):

- `capture_baseline.py` — snapshots production into canonicalized parsed JSON.
- `compare_verification.py` — re-fetches live, deep-diffs against the baseline
  with explicit tolerance, and emits a per-artifact verdict table.

---

## 1. Baseline capture

`capture_baseline.py OUTDIR` fetches a fixed artifact set and stores **parsed,
key-sorted JSON**, so later comparisons are semantic, not byte-level (the orjson
migration changes bytes, never values). Mirror each baseline to
`gs://statbotics-staging-site/verification/baseline-<date>/` so it survives the
scratchpad.

Artifact set (extend, never shrink, so old baselines stay comparable):

| Group | Artifacts |
|---|---|
| Year stats | `/v3/year/{2005,2016,2024,2025,…}` — one pre-2010 year, one mid-2010s, plus the last two completed seasons |
| Leaderboards | `/v3/team_years?year={Y}&limit=50&metric=epa` for two historical years |
| Matches | `/v3/matches?event={key}` for one recent and one old event (2025casj, 2016nytr) |
| Site payloads | `/v3/site/team_years/{last two completed years, CURR_YEAR}` |
| Canary teams | `/v3/team/{254,1678,2056,148}` + site blobs `team/254`, `team/1678` (full `team_years` history) |
| Blobs | `manifest.json` (content-hash fingerprint of all ~4,100 published blobs), `teams/all`, `team_years/{CURR_YEAR}`, `events/{CURR_YEAR}`, `team_to_events` |

Blob fetches resolve logical names through `manifest.json` (versioned
`v2/<name>.<hash>` paths) and zlib-decompress before storing. An `INDEX.json`
records per-item ok/error status; a capture with errors on required artifacts is
not a usable baseline.

**When to capture:** immediately before any deploy that touches the compute or
serving path, and immediately after any intentional data change (re-ingest,
`make reprocess-year`) so the baseline reflects blessed values.

## 2. Historical value parity — the exact-match standard

Everything for a **completed season** is deterministic: EPA is a full-season
replay of a frozen match set ([DATA-REFRESH.md §2](../rig/DATA-REFRESH.md)), so
the standard is **exact equality**. The comparator uses tolerance 1e-9 for
numeric fields, but any observed `max abs diff > 0` on a historical artifact
deserves a look — the 2026-07-21 run matched at exactly 0.0 across ~425k
historical fields, and that is the expected steady state.

Comparator rules (implemented in `compare_verification.py`):

- Deep-recursive diff over parsed JSON; count every field compared, track max
  abs diff, list differing paths. Report a table: artifact / fields compared /
  max abs diff / verdict.
- **Key lists by identity, never position**: team_years by `team`, matches by
  `key`, team history by `year`. List order across blob vs API differs by
  design and is not a defect.
- Skip volatile subtrees on historical site payloads: `competing.*` references
  the *next* event and legitimately changes.
- Canary-team histories compare every year **except CURR_YEAR**.
- `/v3/team/{n}` career fields are informational, not pass/fail:
  `norm_epa.recent` averages the last five years **including CURR_YEAR**
  (`backend/src/data/epa/main.py:43-49`), so it legitimately drifts ±1 while a
  current-year event is live.

**Classifying a historical diff.** Data-driven vs computation-driven, decided
mechanically: if the *match set* changed (keys added/removed — a TBA
correction or re-ingest), the diff is data-driven; if the same matches produce
different EPA/ranks/percentiles, it is a **computation defect** — stop and
bisect the deploy. **Re-baseline** only after a diff is classified as
data-driven and accepted, or after an intentional historical reprocess; never
re-baseline to make a red run green.

## 3. Current-year invariant suite

`CURR_YEAR` values move hourly, so byte-parity is meaningless mid-season.
Instead, invariants that must hold at any instant:

1. **Blob == API.** `team_years/{CURR_YEAR}` blob parsed-equal (keyed by team)
   to `/v3/site/team_years/{CURR_YEAR}`. Both derive from the same
   `process_year()` publish, so any divergence means split-brain in the serving
   path.
2. **Percentile monotone vs rank**, per scope (`total`, `country`, `state`,
   `district`) and per group within scope: sorting by rank, percentile is
   non-increasing; rank 1 holds the group's max percentile.
3. **Event blob EPA == API EPA** for at least two live-or-recent events picked
   from `events/{CURR_YEAR}` (status `Ongoing` first, then most recently
   `Completed`): every team's `epa.{total_points,unitless,norm}` and full
   breakdown in blob `event/{key}.team_events` equals
   `/v3/team_events?event={key}`.
4. **Coverage.** Every CURR_YEAR team with `record.count > 0` appears in
   `teams/all`; `/v3/team/{n}.norm_epa` is non-null for a rank-stratified
   sample (top/middle/bottom 20). Note: the `teams/all` blob carries only
   `{active, name, team}` by design — norm checks go through the API.
5. **Distribution sanity.** Norm EPA median and mid-50% mean within 1400–1600
   (≈1500 by construction), norm within [1000, 2400], unitless within
   [800, 2600], and median within ±25 of the previous baseline's.

## 4. In-season / during-event procedure

During an active event, EPAs change every cycle (~5 min under the read ping,
hourly idle — [DATA-REFRESH.md §3](../rig/DATA-REFRESH.md)). Verification then
works by **invariants plus before/after single-cycle deltas**:

1. Snapshot: `python3 capture_baseline.py snap_t0` (~1 min).
2. Force one cycle and wait for it:
   `curl -s $API/v3/site/update_curr_year` — `{"skipped"}` means no TBA change
   (assert snapshots equal); otherwise poll `$API/info` / the manifest hash
   until the publish lands.
3. Snapshot again: `python3 capture_baseline.py snap_t1`, then run the delta
   checks (`compare_verification.py` pointed at `snap_t0`, plus the delta
   rules below):
   - **A team's EPA moves only if it played.** Join `/v3/matches?event={key}`
     between snapshots; every team whose CURR_YEAR EPA changed must appear in a
     newly completed match of a rating-eligible event (offseason events are
     frozen and must move **no** ratings — `skip_update` in
     `backend/src/models/template.py`; only predictions/schedules refresh).
   - **Ranks re-sort consistently**: recompute ranks from the new EPA values;
     they must equal the published ranks; team_count constant unless a team
     debuted.
   - **Blob == API after the cycle** (invariant §3.1 re-run on `snap_t1`).
   - Historical artifacts in the two snapshots are byte-identical — a live
     cycle must never touch a completed season.
4. **Daily tier check** (once per day in season): every completed current-year
   event's TBA manifest entry has `last_validated` within 24 h
   ([tba-cache spec §2.3](2026-07-20-tba-cache-design.md)), and the
   `statbotics-tba-sweep` job (05:30 UTC) succeeded — check Cloud Scheduler
   status via `make smoke` / console. A sweep 200 (real TBA change) triggers a
   historical reprocess, which mandates a §2 run and re-baseline.

## 5. Cadence

| When | What |
|---|---|
| **Every deploy** (part of the [DEPLOY.md §2](../rig/deploy/DEPLOY.md) checklist) | Capture baseline pre-deploy → deploy → full §2 + §3 run. A DEFECT blocks calling the deploy done. |
| **Weekly during season** | §3 invariants + one §4 single-cycle delta, against the most recent blessed baseline. |
| **After any sweep-triggered historical reprocess** (or manual `make reprocess-year`) | §2 on the affected year; classify diffs (expected: data-driven only); re-baseline. |
| **After `make reprocess-curr-year`** | §3 invariants. |

Results always land in a `verification-results.md` with the per-artifact table
and an explicit defect count; the 2026-07-21 run
(22 checks, ~648k fields, 0 defects) is the reference format.

## 6. Non-goals

- No frontend rendering checks — this harness proves the data plane only.
- No load/latency assertions — perf has its own audit
  ([PERF-REPROCESS.md](../rig/PERF-REPROCESS.md)).
- No upstream (statbotics.io) comparison: upstream runs different data
  freshness; parity with upstream is a product question, not a correctness one.
