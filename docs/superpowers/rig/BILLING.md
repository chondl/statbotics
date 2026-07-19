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
  `statbotics-web` (1 vCPU / 1 GiB, min 0 / max 2) — both scale to zero.
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
