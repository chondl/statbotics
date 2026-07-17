# Offseason Event Support — Design Spec

**Date:** 2026-07-16
**Target:** chondl/statbotics fork → staging branch → mirror (statbotics.iterativerefinement.com)
**Status:** Approved design, pre-implementation
**Related:** [2026-07-09 EPA consistency & bucket-first serving spec](2026-07-09-epa-consistency-and-bucket-first-serving-spec.md)

## 1. Goal

All TBA offseason events (event_type 99) for 2025+ appear on the mirror like
regular events — schedules, match predictions, live results — with team EPA
ratings **never** updated by offseason matches. 58 offseason events exist on
TBA for 2026 (March–October); ~30 are still upcoming, including IRI
(2026iri, starts 2026-07-16) and Chezy Champs (2026cc, 2026-09-18).

Urgency: IRI starts the day of this spec. Ship the easiest proven design.

## 2. Research findings (verified 2026-07-16)

- **Upstream history:** offseason support was added (PR #143, 2022), dropped
  (v3 migration #358, 2024), re-added for year ≥ 2025 (PR #400, June 2025,
  commit `d9caad5`), and removed again (PR #412, commit `169330e`,
  June 11 2026). This design restores the PR #400 behavior, adapted to the
  bucket-first/DuckDB stack.
- **Removal motive** (maintainer on Chief Delphi, Statbotics 2026 thread
  post 104): maintenance burden — "a lot of edge cases and coverage time for
  a small fraction of the total usage." Not a data-corruption or crash issue.
  Community reaction was mild pushback (offseason pages were valued).
- **EPA policy:** offseason matches have never updated EPA in any era of
  Statbotics. PR #400: `skip_update = offseason_event or ...` where
  `offseason_event = event.week == 9`. Offseason matches were also excluded
  from season W-L records (`data/wins.py`).
- **Proven end state:** production GCS bucket still serves `event/2025cc`
  (84 matches, full EPA predictions, week 9) — the exact output shape this
  design reproduces for 2026.
- **Known trap:** `2026isrtp` (official Israeli district event, mistyped as
  offseason on TBA) is overridden to `EventType.DISTRICT` via
  `EVENT_TYPE_OVERRIDES` and DOES update EPA. The restore must preserve
  override precedence.

## 3. Decisions

| Decision | Choice |
|---|---|
| Event scope | All type-99 events, year ≥ 2025, with PR #400 quality filters |
| Preseason (type 100) | Excluded (unchanged from current master) |
| EPA policy | Frozen: predictions computed from current EPA; no rating updates; excluded from W-L records |
| Branch strategy | Clean reviewable PR branch off master on the fork, then merge to staging and deploy |
| Sandbox per-event EPA | Deferred (future work, §9) |
| API keys | Not needed, not built. DuckDB serving is efficient enough; upstream's nascent `apikey.tsx` stays untouched |
| Freshness | Slow cron baseline + read-triggered targeted refresh (§7) |

## 4. Backend ingestion

In `backend/src/tba/read_tba.py` (`get_events()`), restore what `169330e`
removed, guided by the `d9caad5` diff:

- Event type 99, year ≥ 2025 → ingest as `EventType.OFFSEASON`, `week = 9`.
  Restore source is the final pre-removal state (`169330e^`, i.e. commit
  `7a3f7ec` "handle offseason events", March 2026) — NOT the initial PR #400
  version, which used a REGIONAL/week-8+1 hack before the enum was
  activated. The `OFFSEASON` enum value is still present on master. The
  pre-removal code also admitted preseason (type 100) as week 0; per user
  decision we keep type 100 dropped.
- `EVENT_TYPE_OVERRIDES` checked first and wins (keeps `2026isrtp` a
  DISTRICT event that updates EPA). `EVENT_BLACKLIST` still applies.
- Quality filters (all from PR #400):
  - fewer than 6 real teams → drop event
  - any placeholder team (`PLACEHOLDER_TEAMS`, 9970–9999) in matches → drop event
  - any B-team (e.g. `frc254B`) in matches → drop event (breaks integer
    team-key parsing)
  - zero matches available one day after the event's end date → drop event
- Offseason events on TBA have `week: null`; the week-assignment logic must
  set week 9 before the "no week → drop" rule fires.

## 5. EPA freeze and aggregates

- `backend/src/models/template.py`: restore
  `offseason_event = event.week == 9` as a `skip_update` condition.
  Predictions are computed and stored per match; post-match EPA equals
  pre-match EPA.
- `backend/src/data/wins.py` (or current equivalent): exclude week-9 matches
  from team season records, as PR #400 did.
- Aggregates (verify, no change expected):
  - `epa_pre_champs` uses `week < 8` — unaffected.
  - end-of-season `epa` (last post-match value) — unchanged because skipped
    updates leave the rating identical.
  - `epa_max`, `norm_epa` — must show no week-9 movement.
- Teams at an offseason event with no team-year row for that season
  (didn't play officially): handle as PR #400 did — pin exact behavior from
  the `d9caad5` diff during planning.

## 6. Frontend

Restore the two removals from `169330e`:

- `frontend/src/pagesContent/events/summary.tsx`: week label shows
  "Offseason" for week 9.
- `frontend/src/components/filterConstants.tsx`: re-add
  `{ value: 9, label: "Offseason" }` to `weekOptions`.

No other frontend changes: event lists already split
ongoing/upcoming/completed by date; event/match pages render whatever the
event blobs contain.

## 7. Freshness — match schedules within ~5 minutes of TBA

> **Shipped behavior differs from the primary design below.** The code shipped
> this section's own documented *fallback*: `GET /v3/site/ping/event/{key}` (not
> `POST /v3/ping/event/{key}`), a **global** 300s cooldown + single-flight (not
> *per-event*), triggering the **existing full `update_curr_year` cycle** (not a
> targeted single-event refresh). The offseason EPA freeze keys on
> `event.type == EventType.OFFSEASON` (not `event.week == 9`). For the mechanism
> as built and running, see [DATA-REFRESH.md](../rig/DATA-REFRESH.md); this
> section remains for design rationale.

Requirement: when TBA publishes an event's match schedule (typically
morning-of) or new results, viewers should see them within ~5 minutes —
but only while people are actually looking.

Insight (user): page views are the demand signal. If nobody requests an
event, staleness is harmless; if people are hitting it, refresh fast.

Wrinkle: on the bucket-first stack, event pages are served from GCS blobs
(via Cloudflare) and may never touch the API — so the view signal needs a
deliberate path.

Design:

- **Slow cron baseline (existing):** the ETL cycle continues as the
  correctness backstop, refreshing all current-year events on its schedule.
  Verify during implementation that the cycle's TBA fetch covers offseason
  events and that no offseason-mode config (cf. upstream
  `offseason_dispatch.yaml`) throttles the mirror.
- **Read-triggered fast path (new):**
  1. Event page fires a fire-and-forget ping (e.g.
     `POST /v3/ping/event/{key}`) when rendering an event whose window is
     active (start date − 1 day … end date + 1 day). Outside the window, no
     ping — historical pages stay purely static.
  2. The API records the ping and, if the event's last refresh is older than
     the cooldown (5 min), performs a **targeted refresh**: fetch that one
     event's matches from TBA, compute predictions from existing (frozen)
     EPAs, republish that event's blob.
  3. **Thundering herd control:** per-event single-flight (concurrent pings
     coalesce onto one in-flight refresh) + per-event cooldown timestamp.
     Both are pure in-process state — the writer is a single instance by
     design (busy-guard from the bucket-first work). The ping hot path
     (cooldown hit / refresh in flight) is a dict lookup returning 204: no
     database, no GCS, no file I/O (user requirement: bursts of viewers
     must be handled quickly and cheaply, and the relational DB must stay
     off the fast path — DB costs scale badly). The refresh itself is also
     DB-free: TBA fetch → predictions from in-process EPA state → blob
     publish; the relational DB only sees offseason data via the slow cron.
- The targeted refresh is cheap precisely because offseason EPA is frozen:
  no season recompute, no cross-event effects — one TBA call, one blob
  publish. If implementation reveals the targeted path is awkward, fallback
  is triggering the existing full cycle behind a global cooldown, at the
  cost of coarser latency.

## 8. Verification

- **Unit tests:** ingestion filter matrix (type 99 admitted at week 9 for
  year ≥ 2025; type 100 dropped; override beats type-99 skip; placeholder /
  B-team / <6-team / matchless events dropped); skip_update leaves EPA
  bit-identical across a week-9 match; week-9 matches absent from W-L
  records.
- **Rig verification:** run the pipeline for 2025 and compare `2025cc`
  output against the known-good production bucket blob (84 matches,
  predictions present, week 9); run 2026 and confirm offseason events
  appear and `2026isrtp` still updates EPA.
- **API sanity pass (user request):** after deploy, spot-check the mirror's
  v3 API for parity between offseason and regular events — e.g.
  `/v3/event/2026iri`, `/v3/matches?event=2026iri`, `/v3/event/2025cc`
  against a real regular-season 2026 event (pick a champs-division key from
  the DB at test time): same shape, sane predictions, no 500s. Also confirm
  regular-season endpoints are unchanged.
- **Aggregate invariant:** for a sample of teams that played 2025 offseason
  events, end-of-season EPA / `epa_max` / `norm_epa` identical before and
  after the change.
- **Live check:** after the staging deploy and a full recompute cycle,
  2026iri renders on the mirror with schedule and predictions; ping-driven
  refresh observed to pick up new TBA data within 5 minutes.

## 9. Rollout

1. PR branch off master on the fork (`gh pr create --repo chondl/statbotics`),
   reviewable like the other 12 PRs.
2. Merge to `staging`, deploy via the reproducible deploy scripts.
3. Trigger a full recompute cycle → 2025 + 2026 offseason events ingest and
   publish (staging's historical load ran under post-#412 code, so 2025cc /
   2025iri are likely missing today and will appear).
4. Run the verification list (§8).

Upstream submission is explicitly out of scope: this reverses the
maintainer's June 2026 decision, so offering it upstream (e.g. as an opt-in
flag) is its own conversation later. Demonstrated community demand exists
(Chief Delphi 2026 thread posts 110, 115) if that conversation happens.

## 10. Out of scope / future work

- **Per-event sandbox EPA** (user's from-scratch idea): ratings evolve
  within an offseason event to reflect its modified circumstances, without
  touching the team's real EPA. Deferred by explicit user decision.
- Preseason (type 100) / week-0 events — relevant next February.
- API keys / auth — deliberately not built.
- Upstream PR for offseason restore.
