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
- **Tables appeared 2026-07-19 09:55**, but stayed empty for ~36 h. Table names:
  - Standard: `billing_export.gcp_billing_export_v1_01DAA6_73BA7B_54EFE1`
  - Detailed (resource-level): `billing_export.gcp_billing_export_resource_v1_01DAA6_73BA7B_54EFE1`
- **It WAS retroactive** — correcting an earlier note here. Once rows landed,
  the standard export carried usage back to **2026-07-10**, the project's first
  day, not just from the 2026-07-19 enablement date. There is no gap needing
  the Console.

### Reading it: `make bill`

Don't hand-roll the SQL — the wrapper (deferred in the first draft, added
2026-07-27) lives in [deploy/Makefile](deploy/Makefile):

```
make bill                 # per-day gross / free_tier / trial_credit / credits / net
make bill DAYS=90         # widen the window (default 30)
make bill-services        # gross per service over the window
```

Both are one physical line of SQL on purpose: macOS ships GNU Make 3.81, where
the `.ONESHELL:` at the top of that Makefile is **silently ignored** (it landed
in 3.82), so a multi-line quoted recipe dies with ``unexpected EOF while
looking for matching `'``.

### The two credit types, and why they are split

`credits` is a repeated field; the export uses **two** types here, and they
expire on completely different terms:

| `credits.type` | `credits.name` | What it is |
|---|---|---|
| `DISCOUNT` | `CPU Allocation Time`, `Memory Allocation Time` | The Cloud Run **free tier**. Renews monthly, permanent. |
| `PROMOTION` | `FreeTrialUpgrade:CreditId-FreeTrial:…` | The one-time **free-trial** credit. Finite. |

**Note there is no `FREE_TIER` credit type** — an earlier draft of this doc said
to look for one, and a query filtering on it returns nothing. The free tier
arrives as `DISCOUNT`.

This split is the whole point of `make bill`: the `gross` column is what the
mirror **would** cost with no credits at all, and when the `PROMOTION` balance
runs out that column becomes the actual invoice.

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

Resources (as of 2026-07-19 — **historical**; Cloud SQL was deleted 2026-07-27,
see [Cloud SQL decommissioned](#cloud-sql-decommissioned-2026-07-27--0281day)):

- **Cloud SQL** `db-f1-micro` + 10 GB PD_HDD — created 2026-07-10, **always on**
  (the only non-scale-to-zero piece). ~$9/mo run-rate; the dominant cost until
  it was decommissioned.
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

Bottom line at the time: **Cloud SQL was the only meaningful recurring cost**
(~$9/mo); everything else is near-free because Cloud Run scales to zero and the
compute fits the free tier. One-time bumps were the historical backfills (GCS
write ops) and image rebuilds/reprocesses. **With Cloud SQL gone there is no
always-on line item left** — every remaining service is either scale-to-zero or
per-use.

## Per-day actuals, 2026-07-10 → 07-26 (from the export)

Every column is dollars. `gross` = pre-credit cost; `net` = what is actually
invoiced.

| Day | Gross | Free tier | Trial credit | Total credits | Net |
|---|---|---|---|---|---|
| 2026-07-10 | 1.97 | −0.14 | −1.83 | −1.97 | 0.00 |
| 2026-07-11 | 0.34 | −0.05 | −0.29 | −0.34 | 0.00 |
| 2026-07-12 | 0.39 | −0.08 | −0.31 | −0.39 | 0.00 |
| 2026-07-13 | 0.37 | −0.06 | −0.31 | −0.37 | 0.00 |
| 2026-07-14 | 0.39 | −0.07 | −0.32 | −0.39 | 0.00 |
| 2026-07-15 | 0.41 | −0.10 | −0.31 | −0.41 | 0.00 |
| 2026-07-16 | 0.40 | −0.09 | −0.32 | −0.40 | 0.00 |
| 2026-07-17 | 1.30 | −0.76 | −0.54 | −1.30 | 0.00 |
| 2026-07-18 | 1.34 | −0.99 | −0.35 | −1.34 | 0.00 |
| 2026-07-19 | 0.36 | −0.03 | −0.33 | −0.36 | 0.00 |
| 2026-07-20 | 0.38 | −0.05 | −0.33 | −0.38 | 0.00 |
| 2026-07-21 | 1.35 | −0.39 | −0.95 | −1.35 | 0.00 |
| 2026-07-22 | 0.44 | −0.08 | −0.36 | −0.44 | 0.00 |
| 2026-07-23 | 0.43 | −0.06 | −0.37 | −0.43 | 0.00 |
| 2026-07-24 | 0.41 | −0.05 | −0.36 | −0.41 | 0.00 |
| 2026-07-25 | 0.52 | −0.12 | −0.40 | −0.52 | 0.00 |
| 2026-07-26 † | 1.01 | −0.28 | −0.73 | −1.01 | 0.00 |
| **Total** | **11.81** | **−3.40** | **−8.42** | **−11.82** | **0.00** |

† Partial. The last day is **always** partial — billing rows lag the export by
a few hours, so never read the final row as a full day.

**Steady-state gross was ~$0.59/day (~$18/mo)** — the mean of the 15 complete
days 07-11 → 07-25, excluding the 07-10 stand-up day. All-in across the window
it is $0.70/day (~$21/mo), which was brushing the $25/mo cap. **Net was $0
every single day**: the free tier covers Cloud Run allocation time and the
free-trial promotion absorbs the rest. $8.42 of trial credit burned in 17 days.

Gross by service over the window: Cloud SQL $5.23 · Cloud Run $3.59 · Cloud
Build $1.81 · Artifact Registry $0.71 · Cloud Storage $0.47 · all else $0.00.

Shape of the spikes — none of them are audience-driven:

- **07-17/18** — IRI weekend. Cloud Run $0.76 and $0.99 against a $0.06
  quiet-day baseline. See [What actually drives cost](#what-actually-drives-cost-reprocessing-and-it-is-match-data-driven).
- **07-21, 07-26** — Cloud Build $0.56 and $0.43: deploys, not data.
- **Cloud SQL was a flat $0.281/day**, every day, forever — the only always-on,
  never-discounted line, and ~48% of steady-state gross. That is what made it
  worth deleting (below).

## Cloud SQL decommissioned 2026-07-27 (−$0.281/day)

DB retirement Phase 4. The instance, its `db-password` secret, the
`cloudsql.client` IAM grant, the `statbotics-seed` DB-mode job, and three stale
DB-era Cloud Run jobs are **deleted**. Expect steady-state gross to fall from
~$0.59/day to **~$0.31/day (~$9.5/mo)**, and the mirror to sit well inside the
cap on Cloud Run + Build + storage alone.

Preconditions verified before deletion — all six, in this order:

1. **Soak** — `DISABLE_DB=True` since revision `statbotics-api-00023`,
   2026-07-21T16:07Z: **5 d 9 h**, against a 48 h bar.
2. **No DB errors** in the api service logs across the entire soak.
3. **No application connections** — `num_backends{database=statbotics3}` peaked
   at 1 in only 10 of 60 sampled hours, irregularly. The hourly cron runs
   24×/day, so an app that still connected would show up in nearly every hour;
   the sporadic single connections are Cloud SQL's own agent.
4. **Parquet complete** — 144 logical objects in the manifest = 24 years × 6
   tables, matching the design's precondition exactly. (2021 is absent by
   design: no standard FRC season.)
5. **Smoke 9/9**, including the `norm_epa` canary, both before and after the
   binding was removed.
6. **Write path proven db-less** — a full `reset_curr_year` ran end to end
   *after* the Cloud SQL binding, `DATABASE_URL`, and `PGPASSWORD` were stripped
   from the service: TBA load → EPA → `Post Wins/EPA/District` → Write Storage →
   Write Snapshot → `refresh_teams`, no errors.

A final `pg_dump` is preserved at
**`gs://statbotics-staging-db-final-export/statbotics3-final-2026-07-27.sql.gz`**
(192 MiB, ~$0.004/mo). It is a **separate, private** bucket with
public-access-prevention on — deliberately *not* `statbotics-staging-site`,
which is world-readable (`allUsers: objectViewer`). Delete it whenever you are
comfortable; the DB is fully reproducible from TBA via a db-less
`reset_all_years` regardless.

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
certainly ~$0 net — the export later confirmed it. **Do not conclude the free
tier is broken without turning the savings toggles on**, or without checking the
`credits` array in the BigQuery export — where the free tier is credit type
**`DISCOUNT`**, not `FREE_TIER`. `make bill` breaks it out for you.

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
