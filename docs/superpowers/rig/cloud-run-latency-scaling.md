# Cloud Run latency & scaling — the rebuild-vs-reads conflict

How the mirror's ETL rebuild interacts with serving latency and Cloud Run
autoscaling, and why DuckDB being fast doesn't save reads during an ingest. This
documents a known characteristic surfaced 2026-07-17; nothing here is a shipped
change — mitigations at the end are options, not done.

## The core issue: one event loop, shared by the rebuild and reads

The backend runs a **single worker**:

```
gunicorn -w 1 -t 1200 -k uvicorn.workers.UvicornWorker   # -t is timeout, not threads
```

That's **one process, one asyncio event loop**. Two facts collide on it:

1. The ingest endpoint is `async def` calling the **synchronous** `update_curr_year(...)`
   directly (`src/data/router.py`) — so the ~1–2 min recompute runs **on the event
   loop** and blocks it (a sync CPU/DB call never yields).
2. The `/v3` read endpoints (`read_event`, `read_match`, …) are **also `async def`**
   and run on the **same** loop.

So while an ingest cycle runs, the loop is stuck inside the recompute and cannot
run any other coroutine — **the DuckDB API reads on that instance stall/queue
until the cycle finishes.** Observed directly: during a manual reprocess, read
`curl`s returned connection errors (`000`) mid-cycle, then recovered.

**DuckDB efficiency does not help here.** Serving `/v3` from DuckDB-over-Parquet
is fast and DB-independent, but the bottleneck isn't the query — it's that reads
and the recompute share one event loop, and the recompute hogs it.

## The freshness model (context)

Live updates come from a fire-and-forget ping on live event pages + the hourly
cron, both funneling into `/v3/site/update_curr_year` (see
[DATA-REFRESH.md](DATA-REFRESH.md)). Two things worth restating because they were
initially mis-stated:

- **One cycle updates *all* events.** `update_curr_year(partial=True)` recomputes
  the whole current year in one pass, so every active event refreshes together.
  More simultaneous events do **not** create more cycles or more per-event work.
- **Bounded frequency.** A global (per-instance) in-memory cooldown caps it at
  **≤1 whole-year cycle per 5 minutes**, and only when TBA actually has new data
  (etag pre-check). So the load does not scale with the number of events — it's
  the same ~1–2 min cycle whether one event or twenty are updating.

Net: the concern is **not** per-event scaling. It's the **read-availability blip**
on the ingesting instance during each cycle.

## Cloud Run autoscaling — does a 2nd instance rescue reads?

Cloud Run decides instance count from **two drivers, whichever needs more**:

- **Request concurrency:** `instances = max(avg concurrent, 1m/10m) ÷ (max concurrency × 60%)`.
  Mirror `containerConcurrency = 80`, so it targets ~48 in-flight/instance.
- **CPU utilization:** targets ~**60%** average CPU.

Mirror bounds: **min = 0** (scales to zero → cold start on first request;
`startup-cpu-boost` on), **max = 2**.

During an ingest on instance A (loop blocked ~1–2 min):

- The recompute is **CPU-heavy** → A's CPU spikes past 60% → the **CPU driver**
  pushes toward a 2nd instance even under light traffic.
- Reads pile up on A as in-flight (A isn't responding) → the **concurrency driver**
  also pushes toward a 2nd instance once they approach ~48.

So Cloud Run **often brings up a 2nd instance (cap 2)** during an ingest, and new
reads route there and get served while A is busy. That makes "reads dead for the
whole cycle" too pessimistic. **But:**

- **Stall window at the start** — scale-up "waits longer at low instance counts,"
  plus a cold start of the heavy 8 GiB container — so reads hitting A stall before
  B is ready (seconds to tens of seconds).
- **Light traffic** may never trip the concurrency driver; the CPU spike might
  still bring up B, but the ~1–2 min cycle can end before B is useful.
- **Hard cap at 2.** If both instances are ever busy (B also catches an ingest, or
  a spike), requests pend up to `max(3.5× startup, 10s)` then **429**.
- **Per-instance ping state.** The ping cooldown/single-flight is in-memory *per
  instance*, so with 2 instances up it isn't truly global — a minor wrinkle that
  can allow a second concurrent probe.

See the pricing/scaling detail: [About instance autoscaling](https://docs.cloud.google.com/run/docs/about-instance-autoscaling),
[Concurrency](https://docs.cloud.google.com/run/docs/about-concurrency).

## Write↔write: a 2nd instance can run a *concurrent* recompute

Everything above is read↔write contention on one instance. Autoscaling adds a
second, subtler collision — two *write* cycles overlapping.

The hourly cron and the ping both funnel into `/v3/site/update_curr_year`, and
nothing locks them against each other beyond the TBA etag pre-check and the
in-memory single-flight (`_ping_inflight` + the 300 s cooldown). On **one**
instance those module globals serialize cycles. But they are per-instance in
memory — so when the autoscaler brings up instance **B** mid-ingest (exactly the
scenario above), B starts with a **fresh** cooldown and can independently fire a
*second* whole-year `update_curr_year` while A is still recomputing. The result is
two concurrent full-season recomputes writing the same DB and racing the single
`manifest.json` write (published **last** each cycle, last-writer-wins). That is a
**correctness** risk, not just latency — the flip side of the "per-instance ping
state" wrinkle noted above.

Manual rebuilds can race the scheduler the same way. The existing safeguard is
`make pause-cron`, which pauses **both** `statbotics-update` (hourly) and
`statbotics-gc` (daily) around a rebuild "so their manifest writes cannot race the
rebuild's," then `make resume-cron`. `reprocess-year` does this automatically;
`reprocess-curr-year` relies on the operator wrapping it (see
[DEPLOY.md](deploy/DEPLOY.md) §3 and [DATA-REFRESH.md](DATA-REFRESH.md) §6).
Mitigation (2) below — a single explicit writer — is what actually removes this
race rather than avoiding it by scheduling.

## Post-publish read blip: the DuckDB manifest re-sync (~30 s)

Distinct from the during-ingest stall: even after a cycle finishes and the loop is
free, newly published data is not instantly queryable. The DuckDB read layer
re-syncs Parquet from the manifest on a TTL, so for up to **~30 s** after a write a
freshly published event can 404/500 ([DEPLOY.md](deploy/DEPLOY.md) §5, "DuckDB
sync TTL ~30 s"). So the freshness path has two latency components in series: the
loop-blocked stall *during* the cycle, then a brief eventual-consistency window
*after* it before the new data is served.

## Cost angle

- A 2nd instance during ingest bills two 2-vCPU/8-GiB instances for the brief
  overlap — a rounding error (see [BILLING.md](BILLING.md)).
- Setting `min-instances ≥ 1` (to kill cold starts) flips a chunk to always-on —
  roughly ~$170/mo per always-warm instance if fully billed — which is why the
  mirror keeps `min = 0` and eats the occasional cold start.

## Mitigations (options, none implemented)

If the read-stall during ingestion ever matters in practice, decouple the
recompute from the request-serving loop:

1. **Offload to a threadpool** — run `update_curr_year` via
   `await run_in_executor(...)` so the event loop stays free to serve reads while
   the recompute runs on a worker thread. Smallest change; keeps one container.
2. **Move ingestion to a separate Cloud Run *job* or service** — the serving
   container never does the heavy work; reads never contend with a rebuild. Also
   fixes the per-instance ping-state wrinkle (one writer, explicit).
3. **Raise `max-instances`** so reads have somewhere to go during ingest — cheap,
   partial (doesn't remove the cold-start stall), and weakens the single-writer
   guarantee unless combined with (2).

Today, with one small offseason event at a time, the blip is tolerable and none
of these is required. Revisit if a busy event (or the regular season) makes the
read stalls visible to users.
