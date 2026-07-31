# Data refresh & EPA computation — how and when

**Authoritative reference for one question: how does the mirror recompute EPA and
re-ingest TBA data, and what triggers it?** This describes the `cph-staging`
branch, which is **what production runs** — not `cph-master` (the minimal
upstream-fix line), which lacks both the ping and the DuckDB/Parquet serving stack
(see [§7](#7-cph-master-vs-cph-staging)).

Verified live against `statbotics-staging` on 2026-07-17 (scheduler config, ping
endpoint, `/info`).

## 1. Design intent

- **While people are actively viewing an event, its match and event pages update
  within ~5 minutes** of TBA posting new schedules or results.
- **When no one is watching, the system refreshes once an hour or less.**

Two triggers deliver this: a **read-triggered ping** (fast path, active use) and
an **hourly scheduler** (backstop, idle). Both funnel into the *same* recompute.
There is no third mechanism — no TBA webhook, no frontend polling, no second
scheduler. Refresh *cadence* is the only lever, and it is set by infrastructure
(Cloud Scheduler frequency + how often pages are viewed), not by the code.

## 2. How EPA is computed — one engine

Every trigger runs the same function: `process_year()` in
[`backend/src/data/main.py`](../../../backend/src/data/main.py). Its steps, in order:

1. `process_year_tba` — fetch teams, events, matches from TBA (etag-cached).
2. `process_year_avg` — year score averages.
3. `process_year_wins` — win/loss/tie records.
4. `process_year_epa` — the model: `calc.py` **sorts every current-year match by
   time and replays each one** through the EPA model; `agg.py` rolls match EPA up
   to event and year; `metrics.py` computes normalized EPA.
5. If `year == CURR_YEAR` — publish compressed blobs to GCS and fold current-year
   Parquet into a single `manifest.json` write; historical years publish their
   Parquet + `hist/` site blobs instead. **GCS is the only store** — the
   pipeline ends here.

**EPA is a full-season replay every cycle — O(season), not incremental.** The
`partial` and `tba_partial` flags change only *how much prior state is loaded*
(a snapshot resume versus a cold rebuild) and the *TBA fetch scope*, never the
EPA math. A partial cycle and a full cycle produce
the same ratings from the same data. (An incremental O(new-matches) design was
proposed in the [2026-04-11 spec](../specs/2026-04-11-duckdb-static-files-rearchitecture-design.md)
and **rejected** — see that file's superseded banner.)

**Offseason freeze.** Offseason matches (`EventType.OFFSEASON`: TBA type 99, year
≥ 2025, week 9) produce predictions but **never move EPA** — `template.py` sets
`skip_update = offseason_event or ...`. They are also excluded from W-L records
and noteworthy leaderboards. `2026isrtp` is overridden to `DISTRICT` via
`EVENT_TYPE_OVERRIDES` and *does* update EPA. So a recompute triggered for an
offseason event refreshes that event's schedule, scores, and predictions while
leaving every rating unchanged.

## 3. When it runs — two automatic triggers, one funnel

Both automatic triggers call the **same probe**,
`GET /v3/site/update_curr_year`, which is a cheap gate:

```
(A) HOURLY CRON                    ┌───────────────────────────────────────┐
 Cloud Scheduler statbotics-update │  GET /v3/site/update_curr_year  (GATE) │
 0 * * * *  ───────────────────────▶  check_year_partial: TBA etag check    │
                                   │   • no change → {"skipped"} (no work)  │
(B) READ-TRIGGERED PING            │   • change → background ───┐           │
 frontend event page               │                            │           │
 GET /v3/site/ping/event/{key} ────▶  300s in-memory cooldown    │           │
 (on page view, event in window)   │  + single-flight ──────────┘           │
                                   └────────────────────────────┬───────────┘
                                                                ▼
                              GET /v3/data/update_curr_year
                              = update_curr_year(partial=True, tba_partial=True)
                              = full-season EPA replay + GCS/Parquet publish
```

### (A) Hourly scheduler — the idle backstop

Cloud Scheduler job `statbotics-update`, cron `0 * * * *`, hits
`GET /v3/site/update_curr_year` (attempt deadline 1800s). This is the correctness
backstop: even with no viewers, every live event refreshes within the hour.
(Companion job `statbotics-gc`, daily 04:30 UTC, garbage-collects unreferenced
blobs — it does not touch EPA.)

### (B) Read-triggered ping — the fast path (added in fork PR #13)

The frontend event page ([`frontend/src/pages/event/[event_id].tsx`](../../../frontend/src/pages/event/[event_id].tsx))
fires `GET /v3/site/ping/event/{key}` fire-and-forget, **on page view**, only for
a **current-year event inside its date window** (`start − 1 day … end + 1 day`).
There is no polling — one fetch per view.

The endpoint ([`backend/src/data/router.py`](../../../backend/src/data/router.py))
is pure in-process memory on the hot path: validate the key regex, check two
module globals (`_ping_last_probe`, `_ping_inflight`); if a probe is in flight or
fewer than 300s have elapsed, return `204` and do nothing. Otherwise flip the
globals, schedule the probe, return `202`. A cold ping self-calls the **same**
`GET /v3/site/update_curr_year` gate as the cron.

Effect: while an event is being viewed, its pages refresh at most once per 5
minutes; when the last viewer leaves, the cron takes over. This buys sub-hourly
freshness during active use **without** running an expensive full-season replay
every few minutes around the clock.

### The shared gate

`check_year_partial` ([`backend/src/data/tba.py`](../../../backend/src/data/tba.py))
does a cheap TBA check and returns a boolean — it fetches nothing into the DB
and computes no EPA. It checks the `{year}/events` list etag first, then, for each
non-completed event inside its date window, the `/matches`, `/rankings`, and
`/alliances` etags. Finally it sweeps **dropped offseason candidates**: type-99
events on TBA that the quality filters kept out of the mirror, currently inside
their date window, get their roster/schedule probed with **unconditional** GETs
(TBA etags are not content fingerprints — the 2026wvrox incident; see
`backend/CLAUDE.md` → offseason filters gotcha), and the gate escalates when one
would now pass the filters. If nothing changed, the gate returns `{"skipped"}`
and no recompute happens — so a ping (or cron tick) over unchanged TBA data is
nearly free. Only a real change escalates to `GET /v3/data/update_curr_year`,
the heavy `process_year` cycle.

## 4. New events are ingested automatically

A genuinely new event appearing on TBA changes the `{year}/events` list etag.
`check_year_partial`'s first check catches that (`if new_etag != prev_etag: return
True # If any new events`), and the partial `process_year` re-fetches the events
list and upserts the new event, then pulls its matches if today falls in the
event window. An offseason event that was on TBA but **dropped by the quality
filters** (empty roster) re-enters through the gate's dropped-candidate sweep
once its roster fills and its date window opens — its probes are refetched
unconditionally, never trusting a 304. **The hourly cron picks up new and
late-filling offseason events on its own** — no manual step required.

The one exception is a **post-deploy backfill**: if you ship new ingest logic for
events that were *already on TBA* before the deploy, their etags are unchanged, so
the gate sees nothing new. Run `make reprocess-curr-year` **once** after such a
deploy to force a fresh full ingest. This is a deploy-time action, not steady
state — see [DEPLOY.md §3](deploy/DEPLOY.md).

## 5. Manual / operator triggers

Heavy recomputes are operator-run via the [Makefile](deploy/Makefile), never
automatic:

| Command | Endpoint | Effect |
|---|---|---|
| `make reprocess-curr-year` | `/v3/data/reset_curr_year` | Full current-year recompute, fresh TBA fetch, **no etag gate**. Picks up events already on TBA. ~4–6 min. |
| `make reprocess-year YEAR=` | Cloud Run job | One historical year via `reprocess_year()`: `process_year(partial=False)` + Parquet + hist blobs + chained current-year re-render. |
| `make update-curr-year` | `/v3/site/update_curr_year` | Same cheap gated path as the hourly cron (manual poke). |
| full history | `reset_all_years` | Rebuild 2002→present. See [historical-backfill.md](../deliverables/historical-backfill.md). |

## 6. Caveats and interactions

- **The ping cooldown is per-process, global, in-memory.** It is correct only
  because the data service runs a **single** gunicorn worker / instance. On
  multi-instance serving the "one probe per 5 min" bound multiplies per process.
  The state resets to "probe on next ping" at every redeploy.
- **The cooldown is global across events, not per-event.** One probe covers all
  in-window events at once (the gate checks them all), so viewing any live event
  refreshes every live event. The [2026-07-16 spec §7](../specs/2026-07-16-offseason-events-design.md)
  first designed a *per-event* targeted refresh; the shipped code took that spec's
  own documented fallback — a global cooldown in front of the existing full cycle.
- **No lock between cron and ping.** Both call the same gate independently; the
  etag check and single-flight narrow but do not eliminate the chance of two
  overlapping cycles.
- **Cadence lives in infrastructure, not git.** Upstream statbotics.io catches
  matches within minutes by running this same probe at a high scheduler frequency
  in-season. The mirror stays hourly and leans on the ping instead.

## 7. `cph-master` vs `cph-staging`

`cph-master` (the minimal upstream-fix line) describes an **older architecture**:
CockroachDB, three separate App Engine services, no ping. **Production runs
`cph-staging`**: no database at all — DuckDB-over-Parquet serving in one Cloud
Run container, the ping, and the offseason freeze. When reasoning about live
behavior, read `cph-staging`. See [STAGING.md](STAGING.md) and [DEPLOY.md](deploy/DEPLOY.md).
