# TBA persistent cache — design

**Status: draft for review (2026-07-20).** Second lever from
[PERF-REPROCESS.md](../rig/PERF-REPROCESS.md): make the TBA fetch step
cache-first with GCS as the persistent store, so rebuilds stop paying ~167 s
per year of serial HTTP. Companion spec:
[DB retirement completion](2026-07-20-db-retirement-completion-design.md) —
this design deliberately gives ETag state a DB-free home.

## 1. Problem

`get_tba()` (`backend/src/tba/main.py`) already has a pickle cache
(`cache/<url>/data.p`) and ETag conditional-GET support, and full rebuilds
already pass `cache=True` for past years. But the cache directory is
cwd-relative on Cloud Run's ephemeral filesystem, so every rebuild starts
cold: a full current-year rebuild spends ~167 s fetching, and full history
~67 min. Measured on real 2025 data: a full year of cached TBA responses is
~32 MB pickled (~5 MB as tar.gz), loads in ~0.25 s, and packs/unpacks in
~0.4 s — the fetch step is free once the cache persists.

ETag state is also fragile today: it lives in the `etags` DB table / the
snapshot's objs tuple, full runs record no ETags at all (`clear_year` even
deletes the year's rows), and partial snapshots retain only the paths touched
that cycle. There is no revalidation policy — cached old-year data is trusted
forever, so late corrections to past events never land.

## 2. Design overview

**GCS becomes the persistent TBA cache.** One archive per year plus one global
archive, each carrying a manifest that holds per-path ETag and
last-validated state. A three-tier freshness policy decides when a cached
path is trusted versus revalidated with a conditional GET.

### 2.1 Storage layout

In the existing site bucket (`GCS_BUCKET`):

```
tba-cache/2025.tar.gz     # events/2025, districts/2025, event/2025*/…, district/2025*/teams
tba-cache/global.tar.gz   # teams/{0..49} pages (not year-scoped)
```

Each archive contains the pickle files in today's `cache/<url>/data.p` layout
plus a `manifest.json`:

```json
{ "event/2025iri/matches": {"etag": "W/\"abc\"", "last_validated": "2026-07-20T14:00:00Z"},
  ... }
```

Year attribution of a cache key: `events/{year}` and `districts/{year}` from
the path segment; `event/{key}/…` and `district/{key}/teams` from the
4-digit year prefix of the key; `teams/{page}` → global. (Verified: these are
the only URL families fetched anywhere.)

### 2.2 Runtime flow

- **Hydrate:** at the start of a pipeline run (`process_year` /
  `reset_all_years` per-year loop / the `reprocess-year` job driver),
  download and extract the year's archive plus `global.tar.gz` into the local
  cache dir if not already present this process. ~1 s per year.
- **Fetch:** `get_tba()` consults the manifest instead of the bare
  file-exists check. Per the tier policy (§2.3) it either serves the pickle
  directly, or issues a conditional GET (`If-None-Match`) — a 304 refreshes
  `last_validated`; a 200 rewrites the pickle, ETag, and `last_validated`.
- **Persist:** at the end of the run, re-pack and upload any archive whose
  contents changed (compare a dirty flag, not bytes). Upload is atomic per
  archive (single-object PUT).

### 2.3 Freshness tiers (user policy, 2026-07-20)

| Tier | Scope | Policy |
|---|---|---|
| **Active** | current-year events, `start−1d` … `end+3d` | conditional GET every cycle (grace extended from today's +1d to +3d: corrections cluster right after events) |
| **Completed, current year** | past the grace window | trust cache; revalidate once per day |
| **Past years** | everything year < CURR_YEAR | trust cache; monthly revalidation sweep + manual force flag |

- The **daily tier** piggybacks on the existing hourly cycle: any current-year
  manifest entry with `last_validated` older than 24 h gets a conditional GET
  that cycle. With ~60–120 completed events by season's end this is at most a
  few hundred 304s per day, spread across 24 cycles.
- The **historical sweep** runs as a daily scheduled job (Cloud Scheduler →
  `/v3/data/revalidate_tba`) that revalidates exactly **one historical year
  per day**, round-robin, with **serial** requests (no parallelism) — TBA
  etiquette per user decision 2026-07-21. ~1,000 conditional GETs per day,
  almost all 304s; every year gets revisited roughly monthly. If any path in
  the swept year returns a 200 (data actually changed), that year is
  reprocessed (`process_year` full, Parquet republish).
- **Manual force:** `make reprocess-year YEAR=… REFRESH_TBA=1` (and a query
  param on the endpoint) bypasses the manifest for that year — every path is
  fetched unconditionally and the archive rebuilt. For known corrections.

### 2.4 ETag state moves to the manifest

The manifest becomes the canonical ETag store, replacing both the `etags`
DB table and the snapshot's `objs[5]` element. `check_year_partial` (the
probe behind `/v3/site/update_curr_year`) reads the manifest instead. This
removes the DB table from the pipeline (feeding
[DB retirement](2026-07-20-db-retirement-completion-design.md)) and fixes two
standing defects: full runs currently wipe ETag state, and partial snapshots
retain only last-touched paths. The `ETag` ORM model and `objs[5]` are
retired together with a compatibility window: during rollout the pipeline
writes both, reads manifest-first.

## 3. Defects fixed in passing

These are pre-existing sharp edges in the fetch layer that this work touches
anyway; each gets fixed (with a test where practical):

1. **Shared-session header leak** — `_get_tba` sets `If-None-Match` on the
   module-level `Session` and never clears it; every later request carries a
   stale ETag. Headers move to per-request.
2. **Alliances ETag bug** — `check_year_partial` passes the *rankings* ETag
   to the alliances check (`src/data/tba.py:132`), so `/alliances` changes
   are mis-detected. Look up the right path.
3. **Cache-hit hides ETags** — a cache hit returns `etag=None`, so hits can
   never refresh stored state. The manifest carries ETags independently of
   the hit/miss path.
4. **cwd-relative cache dir** — becomes an absolute path from an env var
   (`TBA_CACHE_DIR`, default `/tmp/tba-cache`).
5. **`dump_cache` swallows `OSError` silently** — log on failure.
6. **ETag-path/cache-key mismatch** — manifest keys are the URL cache keys,
   ending the `{event}/teams` vs `event/{key}/teams/simple` divergence.

## 4. Interaction with other levers

- **Parallel fetch (PERF-REPROCESS §2.1)** stays worthwhile but its job
  shrinks to revalidation sweeps and cold misses. Build the cache first; the
  thread pool then wraps only the conditional-GET batches.
- **Offseason quality filters** (`read_tba.py:104-135`) issue nested
  event-teams/matches fetches during events-list parsing; these cache under
  normal event paths and need no special handling.
- `2021` is skipped by `reset_all_years`; the archive simply never exists.

## 5. Expected performance

| Cycle | Today | After |
|---|---|---|
| Full current-year rebuild, TBA step | ~167 s | ~2–5 s (hydrate + in-window conditional GETs) |
| Full history (2002→present), fetch total | ~67 min | ~15–20 s warm; cold unchanged (then warm forever) |
| Hourly partial cycle | ~5 s TBA | unchanged (already ETag-gated) |
| Monthly sweep | — | a few minutes, or spread across days |

## 6. Implementation order

Independently shippable PRs to `cph-staging`, each verified with `make smoke`
plus the parity check in §7:

1. Fetch-layer hygiene: per-request headers, alliances-ETag fix, absolute
   cache dir, OSError logging. (No behavior change beyond bug fixes.)
2. Manifest + GCS archives: hydrate/persist, manifest-backed `get_tba`,
   dual-write ETags (manifest + objs[5]).
3. Tier policy: grace extension, daily revalidation in the hourly cycle,
   `REFRESH_TBA` force flag.
4. Daily one-year sweep endpoint + Cloud Scheduler job + changed-year
   reprocess (serial requests).
5. Retire `objs[5]`/ETag ORM reads (after DB retirement lands or with it).

## 7. Verification

- **Parity:** reprocess a reference year (2025 and 2026) cache-cold vs
  cache-warm; hash the per-team EPA trajectories and the published Parquet —
  must be identical.
- **Freshness:** integration test faking a TBA 200-after-304 for a completed
  event; assert the daily tier picks it up within one simulated day and the
  monthly sweep triggers a year reprocess.
- **Ops:** after deploy, observe one hourly cycle's log — hydrate time,
  conditional-GET count, archive upload — and record numbers in
  [RIG.md](../rig/RIG.md).

## 8. Resolved questions

- ~~Sweep pacing~~ **Decided 2026-07-21: serialized, one historical year per
  day** (see §2.3). No parallel sweep bursts. Net TBA load goes *down*
  overall: full rebuilds stop refetching everything, and the sweep's daily
  serial trickle is far below today's rebuild traffic.
- Current-year archive is the source for `reset_curr_year` (trust + tiers),
  including the cold first post-deploy run, which self-heals. (Confirmed.)
