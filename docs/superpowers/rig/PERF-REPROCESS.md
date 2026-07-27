# Reprocessing performance: analysis & improvement plan

## RESULTS — shipped 2026-07-21 (overnight program, PRs #33-#39)

All four specced levers shipped, verified in production, correctness-proven
(~648k fields vs pre-change baseline, 0 defects — see
[verification results](verify/verification-results.md)). All numbers below are
measured production runs, not estimates.

### Path 1: hourly cron / ping (what runs all day during events)

Both triggers funnel into the probe; a no-new-data probe costs a few
conditional GETs and runs no pipeline (unchanged). When fresh data IS found:

| Step | Before (2026-07-20) | After (2026-07-21) | Lever |
|---|---|---|---|
| Read Snapshot | ~5.8 s | ~3.3-5.3 s | pickle+zstd |
| TBA fetch | 28-33 s | ~21-29 s | by design: in-window events get conditional GETs every cycle (your freshness policy) + daily-tier 304s |
| EPA replay | ~15.3 s | ~15-19 s | untouched (next lever) |
| Write Snapshot | ~33.5 s | ~4-7 s | pickle+zstd |
| Write Storage | 36-38 s | ~10-13 s | team-blob gate + orjson |
| Write DB | ~10 s | 0 | retirement |
| **Total** | **~2m10s** | **~70 s** | |

During an event, that ~70 s is the refresh latency after each probe hit.
Remaining budget is EPA (~19 s) + TBA checks (~29 s): the EPA inner-loop lever
(§2.4, unimplemented) is the only meaningful lever left for this path;
manifest-backed probe (cache spec item 5) shaves the probe's snapshot read.

### Path 2: full-year reprocess

| Operation | Before | After | Notes |
|---|---|---|---|
| `reset_curr_year` | ~6m32s (Jul 17; ballooned to ~12 min under f1-micro credit exhaustion) | **~2m07s** cold container; TBA 19-42 s archive-warm | no DB write; trust-cache tiers |
| Historical year (`reprocess-year` job) | ~17 min DB mode | **~5m09s** for the year (TBA 3m00 cold-archive seed; warm would be ~20 s) + chained curr-year render | job now defaults db-less; hist blobs from pipeline |
| Full history (2002→present) | ~99 min (never run in prod) | untested end-to-end; per-year costs imply ~15-25 min cold, ~8-12 min archive-warm | via db-less `reset_all_years` |

### Correctness (user priority)

- Historical values EXACT vs pre-change baseline (2005/2016/2024/2025): scores,
  EPA win probs, norms, percentiles, ranks — max abs diff 0.0.
- Offseason EPA freeze verified: 0/3724 teams' 2026 EPA/norm/unitless moved
  across the whole program.
- The only drifts found (2 teams' norm_epa_recent, ±1.0) were pre-existing
  STALE DB aggregates (frozen at the Jul-17 reset) that the program FIXES —
  proven by recomputing from the baseline's own rows.
- Standing harness: [correctness-verification spec](../specs/2026-07-21-correctness-verification-design.md)
  + scripts in [verify/](verify/).

### Still open

- EPA inner loop (§2.4) + columnar (§2.5): next big lever, unimplemented.
- Manifest-backed probe (cache spec item 5): kills the probe's 3 s snapshot
  read + the empty-objs[5]-after-reset spurious cycle. Fast-follow.
- Daily tier: `/teams/simple` suffix omitted (final review LOW-6).
- Phase 4 decommission after soak ends **2026-07-23T16:07Z** (Cloud SQL delete,
  dead-code removal, seed-job replacement).
- Full-history db-less rebuild: run once to seed all year archives (also makes
  the daily sweep non-trivial for all years).

**Status (2026-07-20): analysis complete; first two levers are specced.** See
[TBA persistent cache](../specs/2026-07-20-tba-cache-design.md) and
[DB retirement completion](../specs/2026-07-20-db-retirement-completion-design.md).
This doc
captures a performance audit of the season-reprocess pipeline on `cph-staging`
(the deployed branch). A follow-up session will turn the "Levers" section into a
concrete design + implementation plan. Nothing here is implemented yet.

**Standing decision:** keep **full-season recompute** as the only model. Do NOT
build incremental EPA computation — the numerical core is seconds per season;
the wins are elsewhere, and incremental state adds system complexity for no
payoff. (User decision, 2026-07-20.)

**Standing decision:** DB writes are resolved by **finishing the `db-retirement`
work**, not by tuning the write path. (User decision, 2026-07-20.)

Related docs: [RIG.md](RIG.md) (measured cycle timings),
[DATA-REFRESH.md](DATA-REFRESH.md) (when reprocessing runs),
[deploy/DEPLOY.md](deploy/DEPLOY.md) (how to ship/reprocess).

## 1. Where the time actually goes (measured)

> **PRODUCTION REALITY CHECK (2026-07-20/21, from Cloud Run logs).** RIG.md's
> timings are local-machine numbers; production runs CPU-bound steps ~4–6×
> slower. Measured in production:
>
> - **Fresh-data partial cycle (hourly cron / ping): ~2m10s** (2026-07-20,
>   two samples): Read Snapshot ~5.8 s · TBA 28–33 s · EPA ~15.3 s ·
>   Write Snapshot ~33.5 s · Write Storage 36–38 s · Write DB ~10 s.
>   Probes that find no new data run no pipeline (e.g. the 20:00 slot).
> - **Full current-year rebuild (`reset_curr_year`): ~6m32s** (2026-07-17
>   16:04 run): Load Teams 1.1 s · TBA 2m13s · EPA 14.0 s ·
>   Write Snapshot 23.3 s · Write Storage 2m04s · Write DB 1m05s ·
>   post-process ~31 s.
>
> Use THESE numbers, not the local table below, when sizing production wins.

From [RIG.md](RIG.md) measured timings and DEPLOY.md notes, per full-year build
(local-machine numbers; see the box above for production):

| Step | Time | Notes |
|---|---|---|
| TBA fetch | ~167 s | **serial** HTTP, 3+ requests per event (`src/data/tba.py`) |
| AVG / Wins | <1 s | |
| EPA replay | ~4–6 s | the "core calculation engine" (`src/models/epa/`, `src/data/epa/`) |
| Write Snapshot / Storage / Parquet | 12–35 s | `json` + `zlib` (`src/google/{snapshot,storage,parquet}.py`) |
| Write DB | 22–68 s | best-effort after GCS for curr year; on critical path for `reprocess-year` |

Full history (2002→present) is ~99 min cold — dominated by the same network +
write costs × ~24 years, **not** by EPA math. Partial cycles are ~13–36 s.

Key conclusion: **the numerical core is already cheap.** A Rust rewrite of the
EPA engine would optimize the smallest slice of the pipeline. The big levers are
I/O-shaped.

## 2. Levers, ranked by impact

### 2.1 Parallelize the TBA fetch (biggest win)

`src/data/tba.py:process_year` loops over ~60–200 events sequentially, issuing
matches/rankings/alliances requests one at a time. Every request is independent.

- Thread pool (16–32 workers) over per-event fetches; process results in order.
- Expected: ~2m47s → ~15–20 s per year; cold full-history ~99 min → ~10–15 min.
- Years are independent at the fetch layer too: full-history rebuilds can
  prefetch all years concurrently while replaying EPA sequentially (EPA needs
  prior years only for `start_season` init ratings).

### 2.2 Write path

- **DB writes are a strategic question, not a tuning one.** Serving is
  bucket-first (DuckDB-over-Parquet); Postgres is a fallback. Options: finish DB
  retirement (see the `db-retirement` branch), or skip DB writes on rebuild
  jobs. If keeping them: `COPY` (or much larger batches than `CUTOFF = 1000` in
  `src/db/write/template.py`) for the insert-only `clean=True` path.
- **`changed()` diff gate** (`src/data/utils.py`): uses `nan_safe_eq` — NaN
  fields defeat the fast attrs-eq path and force a recursive `_canonical` walk
  per object (earlier draft said `str(obj)`; that was stale). On full rebuilds
  it compares against an empty dict; moot once DB retirement lands. Batch
  `CUTOFF` in `src/db/write/template.py` is 200 (not 1000 as first written).
- **`attr.asdict()` per row** is recursive; `recurse=False` or a cached
  attrgetter tuple is faster at these row counts.

### 2.3 Serialization

Still `json` + `zlib` everywhere (`snapshot.py:77`, `storage.py:50`):

- `orjson`: ~5–10× faster serialization, **reader-invisible** but not a bare
  drop-in: int-keyed dicts need `OPT_NON_STR_KEYS`, and raw `np.float64`
  reaching `json.dumps` today must be laundered (or `OPT_SERIALIZE_NUMPY`).
  Specced in the site-blob write-path design.
- `zstd`: ~3–5× faster than zlib at equal-or-better ratio, but changes the
  compression format — must be coordinated with frontend/bucket readers.
  Do orjson first, zstd as a separate coordinated change.

### 2.4 EPA inner loop (~5 s → well under 1 s, pure Python/numpy)

The math is a sequential EWMA over matches (can't batch across matches), but
per-match cost is dominated by overhead, not arithmetic:

1. **`keys.index(...)` linear scans** — `post_process_breakdown`,
   `get_score_from_breakdown`, `post_process_attrib`
   (`src/models/epa/breakdown.py`) call `list.index()` dozens of times per
   match on a fixed per-year key list. Resolve all indices once in
   `start_season()` into integer constants. Trivial; ~20–30% of the EPA step.
2. **Tiny numpy arrays (length ≤18)** — numpy per-op dispatch (~1 µs) dwarfs
   the arithmetic. `predict_match` rebuilds arrays from lists per call;
   `Match.get_breakdown` builds arrays from 18 f-string `getattr`s per alliance
   per match. Either drop numpy for tuples at this dimension, or restructure to
   one `(num_teams, 18)` float64 matrix with a team→row map (in-place row
   updates, alliance sums as `matrix[rows].sum(0)`).
3. **Record-keeping dominates the EPA step** — `pre_record_team` /
   `post_record_team` (`src/models/epa/main.py`) run `np.round` and build
   ~19-entry dicts of boxed floats for 6 teams, twice per match, plus
   `setattr(te, f"comp_{i}_epa", ...)` f-string churn. Restructure: write
   pre/post vectors into a preallocated `(num_team_matches, 18)` array during
   the replay; one vectorized `np.round` + one serialization pass at the end.
   Extra payoff on cph-staging: the same blobs are consumed 3× (snapshot,
   Parquet build, storage blobs) — columnar output feeds
   `build_parquet_uploads` almost directly.
4. **`agg.py` O(n²) rank pass** — `sorted_teams.index(team)` inside the
   per-team loop, ×4 (total/country/state/district) ≈ ~30M list scans per year.
   Precompute `{team: rank}` dicts.

### 2.5 Data restructuring

The per-match `pre_epas`/`epas` JSON blobs (`src/db/models/match.py`) are the
worst-structured data: written per match, re-parsed downstream with
`(m.epas or {}).get(str(team), {}).get("epa")` chains in `src/data/epa/agg.py`.
A columnar team-match EPA structure (the matrix from 2.4.3, persisted as typed
columns / Parquet) shrinks storage, speeds every write step, and turns
aggregation into dataframe ops. Aligns with the existing DuckDB+Parquet
architecture; full rebuilds could emit a year's Parquet directly from arrays.

### 2.6 Rust core: feasible, small, but wrong first move

The extractable engine — `process_match` loop (`src/models/template.py`), the
`EPA` class, per-year breakdown adjustments, the 10-line EWMA — is ~600–700
lines with clean numeric I/O. A PyO3 extension over columnar match arrays would
replay a season in single-digit milliseconds. But that saves ~5 s/year in a
pipeline spending minutes on network + writes, and the columnar restructuring
needed to feed Rust efficiently is the same work that gets ~90% of the speedup
in Python. **Rust earns its keep only for parameter sweeps / backtesting**
(hundreds of full-history replays for model development) — revisit if that's on
the roadmap.

## 3. Suggested implementation order

Each independently shippable via PR to `cph-staging`, testable with
`make smoke` + a parity check on a reprocessed year:

1. Parallel TBA fetch (2.1)
2. Full-rebuild write fast path: `changed()` short-circuit + insert-only
   batching, or the DB-retirement decision (2.2)
3. orjson swap (2.3) — zstd later, coordinated with readers
4. EPA index constants + columnar record-keeping + agg rank dicts (2.4, 2.5)

## 4. Open design questions for the next session

- ~~DB writes: optimize, skip-on-rebuild, or finish `db-retirement`?~~
  **Decided 2026-07-20: finish `db-retirement`.** Item 2 becomes "land the
  `db-retirement` branch"; no write-path tuning.
- TBA fetch concurrency: thread pool inside `process_year` vs. an async
  prefetch layer across years for full-history rebuilds; TBA API rate-limit
  etiquette (what concurrency is polite/safe).
- Columnar EPA output: keep the JSON blob *format* for readers and generate it
  from the matrix (compatible), or change the stored schema too (bigger, needs
  reader coordination)?
- Parity harness: how to prove a reprocessed year is bit-identical (or
  tolerance-identical) before/after each perf change — candidate: hash the
  per-team EPA trajectories for a reference year (e.g. 2025 + 2026) and compare.
- Is parameter-sweep/backtesting tooling wanted later? (Decides whether the
  Rust core ever becomes worth it.)
