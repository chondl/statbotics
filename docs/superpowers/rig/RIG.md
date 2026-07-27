# Local Verification Rig

STATUS: READY

Shared local end-to-end environment. It mirrors production: **no database** — a
full 2026 season is computed db-less and published as Parquet tables, a state
snapshot, and site blobs into a fake-gcs emulator; both backend servers run
against that bucket; the shared smoke suite passes 10/10 against it.

> The CockroachDB container the rig used before DB retirement is gone (Phase 4,
> 2026-07-27). The GCS emulator is now the rig's only stateful component, which
> is exactly the production shape.

All paths below are absolute. Rig scripts/config live under
`/Users/chondl/learn/statbotics/docs/superpowers/rig/`. The backend
runs from the **rig worktree** at `/Users/chondl/learn/statbotics/.worktrees/rig`
(branch `rig-local`). Track agents run their OWN worktree's code by pointing
`PYTHONPATH` at their backend (see "Track agents: using the rig" below) — no
tracked files are modified in any worktree.

---

## Topology / connection strings

| Component | Where | Connection |
|-----------|-------|------------|
| GCS emulator | docker `fake-gcs-rig` (`fsouza/fake-gcs-server`) | http://localhost:4443, bucket `site_dev_v1` — holds Parquet, snapshot, and blobs |
| API/site server | uvicorn `main:app` | http://127.0.0.1:8000 |
| Data server (ETL) | uvicorn `main:app` | http://127.0.0.1:8001 |

Bucket name is `site_dev_v1` because the app is run non-PROD
(`src/google/storage.py`: `BUCKET_NAME = "site_v1" if PROD else "site_dev_v1"`).

### Environment (rig.env)

`docs/superpowers/rig/rig.env` — source before any backend command:

```bash
set -a; source /Users/chondl/learn/statbotics/docs/superpowers/rig/rig.env; set +a
export PYTHONPATH=/Users/chondl/learn/statbotics/.worktrees/rig/backend
```

It sets `STORAGE_EMULATOR_HOST=http://localhost:4443` (google-cloud-storage 3.1.0
honors this — `storage.Client()` auto-uses anonymous creds and hits the emulator,
verified) plus a dummy `GOOGLE_CLOUD_PROJECT`. **PROD is intentionally unset**
so the app uses the `site_dev_v1` bucket (`src/google/storage.py`) rather than
the production one. Db-less needs no flag — there is no database code left to
switch on.

### TBA API key

- The backend hardcodes a working public read key in `src/tba/constants.py`
  (`AUTH_KEY`); it returns 200 against TBA v3 (verified) and seeded all of 2026.
- A real user-provided key exists at `/Users/chondl/thebluealliance_api_key.txt`
  (chmod 600, format `X-TBA-Auth-Key=<value>`, value not reproduced here).
  `rig_bootstrap.py` reads it and injects it into the live TBA session at runtime
  (no tracked file edited). `seed.py`, `update.py`, and `serve.py` all import
  `rig_bootstrap`, so every rig TBA call uses the real key when the file is
  present and falls back to the hardcoded public key otherwise.

---

## Start / stop

```bash
# Bring up everything (idempotent — skips what's already running):
/Users/chondl/learn/statbotics/docs/superpowers/rig/start.sh

# Stop backend servers (docker left running, data persists):
/Users/chondl/learn/statbotics/docs/superpowers/rig/stop.sh
# Also stop the docker containers:
/Users/chondl/learn/statbotics/docs/superpowers/rig/stop.sh --docker
```

The fake-gcs container and both uvicorn servers stay up between sessions.
`docker start fake-gcs-rig` restores the container with data intact.
Server logs: `docs/superpowers/rig/logs/server{8000,8001}.log`.

To simulate the production outage (Track 2 acceptance 3.4.1), stop only the
servers (`stop.sh`) and leave the GCS emulator up — pages must render from blobs.

---

## Seeding

Seed first on an empty rig — a partial cycle refuses to run without a readable
state snapshot, so `seed.py` is what makes the bucket usable. A full 2026 build
is roughly 3724 teams, 215 events, 8304 team_events, 18372 matches, ~227 blobs.
To seed or reseed from scratch:

```bash
cd /Users/chondl/learn/statbotics/.worktrees/rig/backend
set -a; source /Users/chondl/learn/statbotics/docs/superpowers/rig/rig.env; set +a
export PYTHONPATH=$PWD
.venv/bin/python /Users/chondl/learn/statbotics/docs/superpowers/rig/seed.py
```

`seed.py` runs `update_curr_year(partial=False, tba_partial=False)` — a full
current-year recompute from a fresh TBA fetch. There is no schema step: the run
writes the year's Parquet tables, the state snapshot, and the site blobs
straight into the emulator bucket, and those artifacts are the only state the
rig has.

## Running one update cycle

Partial cycle (what the tracks measure before/after; mirrors prod
`/v3/data/update_curr_year`):

```bash
cd /Users/chondl/learn/statbotics/.worktrees/rig/backend
set -a; source /Users/chondl/learn/statbotics/docs/superpowers/rig/rig.env; set +a
export PYTHONPATH=$PWD
.venv/bin/python /Users/chondl/learn/statbotics/docs/superpowers/rig/update.py
```

Or over HTTP against the running data server (synchronous):

```bash
curl http://127.0.0.1:8001/v3/data/update_curr_year        # partial=True,  tba_partial=True
curl http://127.0.0.1:8001/v3/data/update_curr_year_debug  # partial=True,  tba_partial=False
curl http://127.0.0.1:8001/v3/data/reset_curr_year         # partial=False, tba_partial=False
```

The pipeline's `Timer` prints per-step durations (Track 1 item D uses these).

### Measured timings (this machine, static 2026 data — **historical**)

Captured before DB retirement, when the rig still ran a local CockroachDB. Kept
only as a dev-machine order-of-magnitude reference; the `Write DB` steps no
longer exist, so re-measure rather than trusting these.

| Cycle | Total | Dominant steps |
|-------|-------|----------------|
| Full build (`partial=False`) | ~230–255 s | TBA fetch ~2m47s (no cache for curr year), Write DB 22–68 s, Write Storage 12–35 s |
| Partial (`partial=True`), warm etags | ~13–36 s | TBA ~5 s, EPA ~4–6 s, Write DB <1 s, Write Storage 2–18 s |

### Measured PRODUCTION timings (Cloud Run, 2026-07-21, post-perf-program)

All from real runs after the four perf features + the db-less flip (16:07 UTC).
Before-numbers are measured production cycles from 2026-07-20/21 pre-change.

| Cycle | Before | After | Dominant steps after |
|-------|--------|-------|----------------------|
| Probe, no new data (cron/ping) | seconds, no pipeline | unchanged | a few conditional GETs |
| Fresh-data partial cycle | ~2m10s | **~70 s** | TBA ~29 s (in-window etag checks + daily tier), EPA ~19 s |
| Full current-year reset (`reset_curr_year`) | ~6m32s (Jul 17) | **~2m07s cold container / archive-warm TBA ~19-42 s** | TBA revalidation, EPA ~18 s, Write Storage ~40 s |
| Historical-year job (`reprocess-year`) | ~17 min (DB mode) | ~5m09s for the year itself (cold TBA 3m00) + chained curr-year render | TBA cold fetch; no DB writes |
| Step: Read Snapshot | ~5.8 s | ~3.3-5.3 s (pickle+zstd) | |
| Step: Write Snapshot | ~33.5 s | ~4-7 s (pickle+zstd) | |
| Step: Write Storage (partial) | ~36-38 s | ~10-13 s (team-blob gate + orjson) | |
| Step: Write DB | ~10 s partial / 1m05-6m32 full | **0 (db-less)** | |

The local table above is retained for dev-machine reference only — production
sizing uses THESE numbers.

---

## Smoke suite (part of the READY bar)

`docs/superpowers/rig/smoke/smoke.py` — stdlib only, no test framework. Any agent
points it at any environment; non-zero exit on any failure.

```bash
python3 /Users/chondl/learn/statbotics/docs/superpowers/rig/smoke/smoke.py \
  --base-url http://127.0.0.1:8000 --data-url http://127.0.0.1:8001 \
  --gcs http://localhost:4443 --bucket site_dev_v1 --year 2026 [--run-update]
```

(All flags default to the values above, so bare `python3 smoke.py` works.)

Checks: (1) liveness `/` + `/info`; (2) API reads `/v3/team/254` (team==254 + EPA
fields), `/v3/site/team_years/{year}` non-empty — served by DuckDB over Parquet;
(3) blob reads `teams/all`,
`team_years/{year}`, one discovered `event/{key}` — fetched, zlib-decompressed,
parsed, non-trivial; (4) consistency probe — a sampled team's EPA in an
`event/{key}` blob equals the same team_event EPA from the site API
(and `team_years` blob == API) within 0.5 pt; (5) `--run-update` triggers a cycle
on the data server and asserts the publish landed via EITHER path:

- **legacy**: `team_years/{year}` blob generation advanced, OR
- **manifest**: `manifest.json` exists and advanced (object generation changed,
  its bytes changed, or it appeared for the first time).

The manifest path exists because Track 2's copy-on-write publishing
intentionally stops re-uploading unchanged blobs each cycle — only the manifest
advances, and check 5 must not fail on that. Both branches are verified: the
legacy branch via live cycles, the manifest branch via fake-gcs object
simulation (generation + content change detection; first appearance counts as
an advance). Still stdlib-only and fully env-parametrized.

Current result against the rig: **10/10 pass** (incl. `--run-update`).

**Consistency-probe note (important for Track 1).** The literal repro in spec
2.4.1 — "for an upcoming event with no matches, event EPA == year EPA" — cannot
be reproduced from static 2026, because every 2026 event already has matches, so
event-specific EPA legitimately differs from year EPA. The smoke probe instead
asserts **blob == API** for the same data, which is the equivalent
regression guard (it catches the exact "event blob served stale while the
Parquet is fresh" bug) and is valid on any data. To exercise the true 2.4.1 scenario, Track
1 must construct a fixture: register teams to a synthetic future/no-match event
(or blank an event's matches) and assert the event-blob EPA tracks year EPA.

---

## Track agents: using the rig

Run YOUR worktree's backend code against the shared rig by overriding
`PYTHONPATH` (the rig venv's installed deps are worktree-independent):

```bash
WT=/Users/chondl/learn/statbotics/.worktrees/epa-consistency   # or bucket-first-serving
set -a; source /Users/chondl/learn/statbotics/docs/superpowers/rig/rig.env; set +a
export PYTHONPATH=$WT/backend
PY=/Users/chondl/learn/statbotics/.worktrees/rig/backend/.venv/bin/python

# serve your code (stop the rig's 8000/8001 first if you want your build there):
PORT=8000 $PY /Users/chondl/learn/statbotics/docs/superpowers/rig/serve.py &
# run a cycle with your code:
$PY /Users/chondl/learn/statbotics/docs/superpowers/rig/update.py
# verify:
python3 /Users/chondl/learn/statbotics/docs/superpowers/rig/smoke/smoke.py --run-update
```

Alternatively create a venv in your own worktree from
`docs/superpowers/rig/requirements-rig.txt` (below). The rig venv uses Python
3.11 (`pyproject` requires `>=3.9,<3.12`).

---

## Gotchas / limitations (verified on this rig)

- **Seed before any partial cycle.** Db-less partial cycles read prior pipeline
  state from the GCS state snapshot and nothing else. On an empty bucket the
  cycle deliberately skips (rather than publishing near-empty artifacts over
  good ones) and says so in the log. Run `seed.py` first.
- **Curr-year-only seed fidelity.** Prior years are empty, so absolute EPA seeds
  differ from prod (fine for gate/diff/publish mechanics; NOT for asserting
  absolute EPA values). Team metadata (names/country/state) is faithful because
  the seed populates teams before team_years — `TeamYear.name` comes from the
  teams table (`src/data/tba.py`). On a truly empty bucket team_years can
  initially show numeric placeholder names; a second `seed.py` run (after teams
  exist) or `refresh_teams` fixes them.
- **fake-gcs `size` field is unreliable** in the object-list JSON (reported tiny
  sizes for full blobs). Verify blob content by downloading + decompressing, as
  the smoke suite does — not by the listed size.
- **Static 2026 = no in-cycle change.** Because 2026 is complete, partial cycles
  produce near-zero deltas and no new `event/{key}` blobs under today's gate.
  To exercise gate/diff/publish logic, mutate the published Parquet or blob
  payloads between cycles (or use the fixture approach noted above).
