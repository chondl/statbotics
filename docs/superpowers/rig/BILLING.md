# Statbotics staging mirror — billing & cost

How the mirror is billed, what a BigQuery cost export was set up, and a rough
estimate of spend to date. GCP project `statbotics-staging` (number
`630091002690`), billing account `01DAA6-73BA7B-54EFE1`.

## BigQuery billing export (set up 2026-07-17 / 2026-07-19)

There is no gcloud/API way to read *actual accrued spend* — pricing and budgets
are queryable, but real cost comes from either the Billing Console → Reports, or
a BigQuery billing export. We set up the latter:

- **Dataset:** `statbotics-staging:billing_export` (location `US`), created via
  `bq mk` on 2026-07-17.
- **Exports enabled** (in the Billing Console → Export → BigQuery, by the account
  owner, 2026-07-19): **both** the **Standard usage cost** export and the
  **Detailed usage cost** export, targeting that dataset.
- **Tables will appear circa 2026-07-20** — the export is not instantaneous;
  first data lands a few hours to ~a day after enabling. Expected table names:
  - Standard: `billing_export.gcp_billing_export_v1_01DAA6_73BA7B_54EFE1`
  - Detailed (resource-level): `billing_export.gcp_billing_export_resource_v1_01DAA6_73BA7B_54EFE1`
- **Not retroactive.** The export only captures costs **from enablement forward**
  (2026-07-19 on). For spend *before* that (the first ~week), use the Billing
  Console → Reports, filtered to project `statbotics-staging`.

### Query to run once data is flowing

Net spend (gross + credits) per service:

```sql
SELECT
  service.description AS service,
  ROUND(SUM(cost), 2) AS gross,
  ROUND(SUM((SELECT IFNULL(SUM(c.amount),0) FROM UNNEST(credits) c)), 2) AS credits,
  ROUND(SUM(cost) + SUM((SELECT IFNULL(SUM(c.amount),0) FROM UNNEST(credits) c)), 2) AS net
FROM `statbotics-staging.billing_export.gcp_billing_export_v1_01DAA6_73BA7B_54EFE1`
GROUP BY service
ORDER BY net DESC;
```

Run with `bq query --use_legacy_sql=false '<sql>'`. A `make bill` wrapper was
**deliberately deferred** (decide later).

## Cost structure (reference)

Cloud Run (us-central1), **request-based** billing — the mirror's default; you
pay only while an instance is actively processing a request, **$0 when idle /
scaled to zero**:

| | vCPU-second | GiB-second | Requests |
|---|---|---|---|
| Request-based (mirror) | $0.000024 | $0.0000025 | $0.40 / million |
| Always-allocated (CPU always on) | $0.0000336 | $0.0000035 | $0.40 / million |

**Free tier / month (us-central1):** 180,000 vCPU-s · 360,000 GiB-s · 2M requests.

At the api service's 2 vCPU / 8 GiB, one *active* instance-second =
`2×0.000024 + 8×0.0000025` = **$0.000068/s** (~$0.24 per active instance-hour) —
billed only for active time.

## Provisioned resources & rough spend to date

Resources (as of 2026-07-19):

- **Cloud SQL** `db-f1-micro` + 10 GB PD_HDD — created 2026-07-10, **always on**
  (the only non-scale-to-zero piece). ~$9/mo run-rate. This is the dominant cost.
- **Cloud Run** `statbotics-api` (2 vCPU / 8 GiB, min 0 / max 2) and
  `statbotics-web` (1 vCPU / 1 GiB, min 0 / max 2) — `minScale=0`, but see
  [Instances rarely scale to zero](#instances-rarely-scale-to-zero-and-that-is-fine).
- **GCS** bucket `statbotics-staging-site` — ~2.17 GB.
- **Artifact Registry** — a few GB of images.
- **Budget cap:** $25/mo on the project.

Estimated spend for the first ~week (2026-07-10 → 07-17), pending the exact
figures from the export / Console:

| Item | ~7-day estimate | Notes |
|---|---|---|
| Cloud SQL `db-f1-micro` + 10 GB | ~$2.00 | Always-on; dominant |
| Cloud Run (api + web) | ~$0–1 | Scale-to-zero; ETL + testing likely inside free tier |
| GCS storage + operations | ~$1–1.50 | Storage pennies; cost is write ops from the historical backfills |
| Cloud Build | ~$0 | Under the 120 build-min/day free tier |
| Artifact Registry | ~$0.10 | Image storage |
| **Total** | **≈ $3–5** | Well under the $25 cap |

Bottom line: **Cloud SQL is the only meaningful recurring cost** (~$9/mo);
everything else is near-free because Cloud Run scales to zero and the compute
fits the free tier. One-time bumps were the historical backfills (GCS write ops)
and image rebuilds/reprocesses.

## Measured actuals (2026-07-20)

First real figures, from the Billing Console (the BigQuery export had still not
populated — tables auto-created 07-19 09:55 but 0 rows 36 h later).

| Service | Jul 1–20 gross |
|---|---|
| Cloud SQL | $3.59 |
| Cloud Run | $2.44 |
| Cloud Build | $0.82 |
| Cloud Storage | $0.40 |
| Artifact Registry | $0.31 |
| **Total** | **$7.57** (July forecast $11.47) |

This validates the estimates above. Cloud SQL accrued $3.59 over ~11 days since
the 07-10 instance creation → **~$10/mo**, matching the ~$9/mo projection.

### Console figures are GROSS unless you enable the savings toggles

The console's **Savings** filter (Other savings / Promotional credits /
Spending-based discounts) defaults to **off**, and the free tier is applied as a
*credit*. So a filtered report shows pre-credit cost, and Cloud Run appears to
cost money even when free-tier usage should zero it out.

Measured July usage was **66,479 vCPU-s** and **259,295 GiB-s**, both inside the
180,000 / 360,000 monthly free tier — so the $2.44 Cloud Run line is almost
certainly ~$0 net. **Do not conclude the free tier is broken without turning the
savings toggles on**, or without checking the `credits` array (credit type
`FREE_TIER`) in the BigQuery export.

### Instances rarely scale to zero, and that is fine

Over a representative 96 h window, from `run.googleapis.com/container/instance_count`:

| | 0 instances | 1 instance | 2 instances |
|---|---|---|---|
| `statbotics-api` | 1.4% | **96.9%** | 1.7% |
| `statbotics-web` | 8.2% | 91.2% | 0.6% |

Despite `minScale=0`, api almost always has a live instance — steady traffic
keeps it from ever idling out. **This costs nothing**: request-based billing
charges only for active request processing, so a warm-but-idle instance is $0.
The `maxScale=2` ceiling is hit only ~1.7% of the time, so it has not been a
practical constraint.

### Careful: `instance_count{state=active}` badly overstates cost

Aggregating `instance_count` with `state=active` implied **43.6 active
instance-hours** for api over 96 h (≈$80/mo). The authoritative metric,
`container/billable_instance_time`, gave **7.2 instance-hours** for the same
window — ~6× less. `instance_count` is a sampled gauge, so any sample where the
instance was busy inflates a whole bucket. **Always use
`billable_instance_time` for cost work.**

Likewise, summing `httpRequest.latency` across requests over-counts: on 07-18
total latency was 42,142 s against only 10,534 s of billable instance time,
because concurrent requests share one instance. Latency-seconds are useful for
*attributing* work between endpoints, never for absolute cost.

## What actually drives cost: reprocessing, and it is match-data-driven

Cost is bursty and tracks live-event activity. Comparing IRI weekend against the
quiet day right after:

| | 07-18/19 (2026iri live) | 07-20 (no live event) |
|---|---|---|
| `/v3/data/update_curr_year` (the heavy reprocess) | **71 calls**, mean 145 s | **2 calls** |
| `/v3/site/ping/event/...` | 850, mean **32 s** | 4, mean **0.0 s** |
| Total latency-seconds | 42,142 s | 772 s |
| api billable instance time | 10,534 s / 14,381 s per day | **365 s** |

Confirmed model:

1. **Reprocess work is automatic, not manual.** The heavy calls come from an
   internal `python-requests/2.32.3` client — the service calling *itself* via
   `update_curr_year_background()` — not from hand-run commands. During IRI it
   fired ~2×/hour instead of the hourly-cron baseline.
2. **New TBA match data creates the work — NOT viewer count.** See below.
3. **With no live event it drops to effectively zero** — 2 heavy calls/day, ~6
   minutes of billable time. The hourly cron still runs 24×/day but mostly
   no-ops in ~1.5 s.

Serving API reads is nearly free by comparison: `/v3/matches` and
`/v3/team_events` average **0.4 s** on a quiet day.

### Cost does not scale with audience

Tempting but wrong conclusion: "850 pings during IRI ⇒ viewers drive cost." Two
hard limits in `backend/src/data/router.py` prevent that:

1. **`PING_COOLDOWN_S = 300`** — a cold ping schedules at most one probe per 5
   minutes; every other ping short-circuits to a 204. Per the code comment, this
   "bounds TBA traffic to one probe per 5 minutes no matter how many viewers
   pile on." Ceiling: 12 probes/hour regardless of audience size.
2. **The probe is etag-gated.** It calls `/v3/site/update_curr_year`, which runs
   `check_year_partial_tba(...)` and returns `{"status": "skipped"}` unless TBA
   actually has new data. Only genuinely new data reaches the heavy cycle.

Observed reprocess rate during IRI was **~2/hour — well under the 12/hour
cooldown ceiling** — so the cooldown was never the binding constraint. TBA data
availability was. Doubling the audience would not have changed the bill; the
pace is set by how often matches are actually played.

### Pings themselves are effectively free

The ping hot path is pure in-process memory (regex + float compare + 204) — no
DB, no GCS, no TBA. Measured:

| | IRI weekend (n=852) | Quiet day (n=4) |
|---|---|---|
| p50 | **0.00 s** | 0.002 s |
| p75 | 60.02 s | — |
| p95 | 122.92 s | — |
| mean | 31.92 s | 0.003 s |

**54.7% of pings returned in under 1 s**, and the 41.3% that exceeded 10 s
account for **99.2%** of all ping latency (26,973 s of 27,193 s). The 32 s mean
is not pings doing work — it is head-of-line blocking behind the ingest.

### Pings block behind the reprocess

Blocked pings finish at the same instant as the reprocess that blocked them
(e.g. 07-18 14:42:31: `update_curr_year` 160.64 s, ping 160.48 s). This is the
single-event-loop conflict documented in
[cloud-run-latency-scaling.md](cloud-run-latency-scaling.md) showing up in the
billing data. It also means **reprocess and serving cost cannot be cleanly
separated** — blocked reads occupy the same instance that is ingesting.

### TODO: split reprocess vs. serving spend

Wanted: a breakdown of instance time spent reprocessing vs. serving API reads.
Note the blocking problem above — a naive per-endpoint latency split will
misattribute blocked reads to serving. Likely approach is to segment
`billable_instance_time` by wall-clock windows where `update_curr_year` is in
flight, rather than to attribute per request.

## Unidentified external poller (first seen 2026-07-18)

An external client has been polling the public mirror continuously since
**2026-07-18 19:17 Z**, and was still running as of 07-21. The mirror was
publicized, so this is presumed to be a third-party agent.

- **UA:** `python-httpx/0.28.1`; arrives via Cloudflare IPs, so the origin is masked.
- **Cadence:** paired requests to `/v3/matches` and `/v3/team_events`
  (`limit=1000`) about every 2.5 min, 24/7.
- **Events:** `2026arc` (1,024 reqs), `2026casnf` (183), `2026sunshow` (4) —
  **all long-completed**, and notably *not* IRI.
- **Share of traffic:** ~73% of api requests in a sampled window.

**Cost impact is negligible** (~$0.03/day, ~$0.75/mo gross) because the reads are
fast and idle time is free. But it is why api never scales to zero, and it will
not stop on its own. If api request volume ever looks inexplicably high, check
for this before assuming a regression.
