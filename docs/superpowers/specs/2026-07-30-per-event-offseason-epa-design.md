# Per-Event Offseason EPA — Design Spec

**Date:** 2026-07-30
**Target:** chondl/statbotics fork → `cph-staging` → mirror (statbotics.iterativerefinement.com)
**Status:** Approved design, pre-implementation
**Supersedes:** §10 "Per-event sandbox EPA" of the [2026-07-16 offseason events design](2026-07-16-offseason-events-design.md), which deferred this work

## 1. Goal

At an offseason event, each team's EPA forks from its current rating, evolves
across that event's matches, and drives that event's match predictions,
rankings, and simulation. The fork touches nothing else: not the team's season
EPA, not its ranks or normalized EPA, not next season's starting rating, and
not any other offseason event.

Today offseason matches are frozen — `skip_update` suppresses every rating
update, so predictions run off season-final EPA for the whole event. That is
measurably wrong: offseason score predictions overshoot by 26 points on
average, and win predictions are worse in playoffs than in quals.

## 2. Evidence

A walk-forward backtest over **61 offseason events and 3,294 completed
matches** (2025 and 2026, pulled from the mirror API) established that the
change improves predictions. Each match was predicted using only offsets
learned from strictly earlier matches, with the production `percent_func` decay
and `ELIM_WEIGHT`.

The harness reproduces shipped metrics exactly. For `2026iri` the frozen
baseline yields RMSE 129.31, bias 50.06, accuracy 0.7778, Brier 0.1522; the
mirror's own `metrics` block for that event reports 129.3143, 50.0554, 0.7778,
0.152. The baseline column is the live system, not an approximation of it.

| Bucket | Metric | Frozen | Sandbox | Δ |
|---|---|---|---|---|
| Elims | accuracy | 0.7224 | 0.7644 | +4.2pp |
| Elims | Brier | 0.1837 | 0.1601 | −12.8% |
| Quals | accuracy | 0.7433 | 0.7721 | +2.9pp |
| Quals | Brier | 0.1719 | 0.1574 | −8.4% |
| All | score RMSE | 75.43 | 62.09 | −17.7% |
| All | score bias | −26.34 | −6.75 | — |

Frozen EPA predicts elims *worse* than quals (0.7224 against 0.7433). The
sandbox closes that gap, and gains more in elims than in quals — the signature
of genuine within-event learning rather than a constant correction.

**Significance.** Brier improves in 49 of 61 events (80%). Match-level McNemar
gives z = 5.43 (p ≈ 6×10⁻⁸): 240 matches flip to correct, 134 flip to wrong.
Bootstrapping over *events* rather than matches — the conservative test, since
matches within an event share teams — puts overall accuracy at +3.22pp, 95% CI
[+1.86, +4.58], and elim accuracy at +4.20pp, 95% CI [+1.16, +7.21]. Both
exclude zero.

**Three limits on the claim.**

Most of the RMSE gain is bias removal, not prediction skill. A control that
applied a single event-wide offset with no per-team learning cut RMSE to 66.94
but left win accuracy and Brier bit-identical to baseline, because a uniform
offset cancels in the margin. Per-team learning is the only source of the
accuracy gain, and that gain is +3–4pp — not −18% of anything.

The change degrades some events. Twelve of 61 lost accuracy; `2025nhgc` fell
from 0.800 to 0.675. The pattern is coherent: the sandbox helps most where
frozen EPA fails badly (`2026audd`, 0.631 → 0.769) and adds noise where frozen
EPA was already good.

`SANDBOX_SEED_COUNT` cannot mitigate that tail. Sweeping it from 0 to 20 held
the worst-event accuracy drop at −0.10 to −0.125 while shrinking the mean gain.
It is a re-tuning hook, not a risk dial.

All of the above comes from a scalar score-offset proxy. The real model updates
an 18-dimensional breakdown vector through `post_process_attrib` and re-derives
scores via `get_score_from_breakdown`, and it also emits RP predictions, which
this proxy never measured. §7's harness re-runs the comparison against the real
model, and §9 gates the merge on that result.

## 3. Decisions

| Decision | Choice |
|---|---|
| Approach | Swap the model's rating state per offseason event (§4); reject a second pass and parallel DB fields |
| Seed value | Copy-on-first-touch from the live global rating at the team's first match of that event |
| Learning rate | `SANDBOX_SEED_COUNT = 0`, a documented and tunable constant |
| Year scope | Every ingested offseason event (2025+); no year branch in the model |
| Team page | Summary line above the horizontal rule, **plus** the event's existing block and match table |
| Figures page | Excludes offseason matches |
| Isolation proof | Season EPA and all downstream aggregates bit-identical before and after (§8) |
| Merge gate | The real-model harness must show sandbox beating frozen (§9) |

`percent_func` on the 2016+ branch multiplies by 2/3, so the true learning rate
spans 0.333 down to 0.200 and clamps at 12 matches. `SANDBOX_SEED_COUNT = 12`
and inheriting a full season's match count are therefore the same setting. The
meaningful range is 0–12, and 0 measured best on RMSE and Brier.

## 4. The fork — `models/template.py`, `models/epa/main.py`

The EPA model reads exactly two pieces of mutable state, `self.epas` and
`self.counts`. Every consumer — `predict_match`, `attribute_match`,
`update_team`, `pre_record_team`, `post_record_team` — goes through them. One
swap therefore covers the whole model, and the EPA math needs no edit at all.

- Add `SANDBOX_SEED_COUNT = 0` to `models/epa/constants.py`, with a comment
  recording the sweep in §2 and the 0.333–0.200 clamp.
- Give `Model.process_match` a context manager. When
  `event.type == EventType.OFFSEASON`, it installs the event-scoped mappings
  held in `self._sandboxes[event.key]` and restores the real ones in a
  `finally`, so an exception cannot strand the model in sandbox state.
  `self._sandboxes` is created empty in `Model.start_season`, alongside the
  existing per-season state, so each season starts with no forks.
- Those mappings copy on first touch: `__missing__(team)` forks
  `EPARating(base[team].mean.copy())` from the real dict and seeds the count at
  `SANDBOX_SEED_COUNT`. Because the base is the real `self.epas` defaultdict, a
  team with no season history still receives the standard `init_rating` — no
  special case for B-teams, demo robots, or teams that skipped the season.
- Drop `offseason_event` from the `skip_update` condition at
  `template.py:91`. Keep `placeholder_match`, `elim_dq`, and `all_fouls`.
- Call `post_record_team` with `ty=None` for offseason matches, so a sandbox
  rating never stamps `TeamYear` component fields.

Keying the sandbox by `event.key` gives cross-event isolation directly: two
offseason events fork independently from the same frozen rating.

`calc.py` replays every match of the year on each cycle, so sandbox state is
rebuilt deterministically every run. Nothing needs to persist, and the pickled
snapshot needs no schema change.

## 5. Containment — `data/epa/agg.py`

The freeze is what makes today's aggregates safe. Removing it opens four paths;
three need code, one is already covered.

1. **`TeamYear` component fields.** `post_record_team` writes `ty` on every
   match (`models/epa/main.py:214-226`). If a team's last match of the year is
   an offseason match, sandbox values land on `ty.auto_epa`, `teleop_epa`, and
   the comps, which feed the year percentiles. Closed by §4's `ty=None`.
2. **End-of-season EPA.** `process_year_epas` (`agg.py:12-23`) derives `ty.epa`
   and `ty.epa_max` from the last and maximum post-EPA across *all* matches.
   Those flow into `norm_epa`, every rank and percentile, `Team.norm_epa*`,
   and — through `all_team_years` → `get_init_epa` — **next season's starting
   rating**. Fix: pass the set of offseason event keys into `process_year_epas`
   and filter those matches out. Use the explicit key set, not a `week == 9`
   test, so it cannot drift from the ingestion convention. `epa_pre_champs`
   already filters `week < 8` and needs no change.
3. **Cross-event `epa_start` chaining.** `curr_epas[te.team] = te.epa`
   (`agg.py:192`) carries one event's final EPA into the next event's
   `epa_start`. TeamEvents iterate sorted by week, so week-9 events land last —
   but a team playing both IRI and Chezy Champs would carry IRI's sandbox
   result into CC. Fix: skip the assignment for offseason team events.
4. **Season W-L records.** Already excluded at `data/wins.py:36`. No change.

`te.epa_start` needs no change either: `agg.py:188` already takes it from the
first `pre_epa`, which under the fork is exactly the seeded value.

`compact_from_match` (`agg.py:26`) gains an explicit `offseason: True` flag.
Offseason entries stay in `ty.team_matches` — the team page still renders their
match table — and the flag is what §6 filters on. `process_year` builds the
offseason event-key set once from `objs[2]` and threads it to both
`process_year_epas` and `compact_from_match`, so items 2 and 3 and this flag
all read the same source of truth.

One accepted consequence: `Event.epa_max`, `epa_mean`, `epa_sd`, `epa_top_8`,
and `epa_top_24` for an offseason event now reflect sandbox finals, which
changes how those events sort in the events list. This is event-local and
intended.

## 6. Frontend

- `pagesContent/team/overview.tsx`: insert a summary line immediately above the
  horizontal rule at line 191 — one per offseason event, naming the event and
  its EPA, labeled so nobody reads it as a rated result. Shape:

  > Offseason: **Chezy Champs** — EPA **88.4** (does not affect season EPA)

  The event name links to the event page, as the per-event blocks already do.
  The number comes straight off the existing `teamEvents` entry
  (`epa.breakdown.total_points`); no new field is required. The section renders
  only when the team played at least one offseason event that year. The
  offseason event keeps its own block and `MatchTable` below, marked as
  offseason.
- `pagesContent/team/figures.tsx`: filter entries flagged `offseason` out of
  `matches` before passing them to `TeamLineChart`.
- Event page, match pages, and `event/[event_id]/worker.ts`: no change. They
  read `TeamEvent` EPA and `match.pred.*`, both of which now carry sandbox
  values, so rankings, match predictions, and the simulation follow
  automatically.

## 7. Backtest harness

A checked-in tool at `backend/src/models/backtest.py`, not a throwaway script.
It loads a year's pipeline objects from the GCS state snapshot
(`src/google/snapshot.py`), which already holds every entity row the model
needs, runs `process_year_calc` with the sandbox on and off, and tabulates
RMSE, bias, accuracy, and Brier per event and per match-index bucket, split by
quals and elims, with a `SANDBOX_SEED_COUNT` sweep. It reuses
`data/epa/metrics.py` for the per-event figures and adds only the bucketing and
the paired comparison. Running it against the local dev rig requires no network
beyond the snapshot fetch.

It reports the event-level consistency count and the event-clustered bootstrap
CI from §2, so a later regression shows up as a shrinking interval rather than
a single moved number.

## 8. Verification

**Cross-season isolation.** Run the full pipeline before and after the change
and assert that every `TeamYear` EPA field, `norm_epa`, rank, percentile, and
`Team.norm_epa*` for **2026 and every later year** is bit-identical. A 2025
offseason match can reach a 2026 rating only through `all_team_years` →
`get_init_epa`, so this tests that path directly.

**Within-season isolation.** The same invariant for 2025 itself: season EPA,
`epa_max`, `norm_epa`, ranks, and the year percentiles unchanged.

**Cross-event isolation.** For a team playing two offseason events in one year,
the second event's `epa_start` equals its season EPA, not the first event's
final sandbox EPA.

**Unit tests.**
- The fork restores real state after a normal match and after an exception.
- `skip_update` still fires for placeholder, elim-DQ, and all-foul matches.
- A team with no `TeamYear` history seeds from `init_rating`.
- `2026isrtp` — an official district event mistyped as offseason on TBA and
  corrected by `EVENT_TYPE_OVERRIDES` — still updates real EPA.

**Accuracy gate.** §7's harness, run against the real model on the full 2025 +
2026 offseason corpus.

**Live checks.** After deploy: `2026iri` and `2025cc` render with sandbox EPA
and sane predictions; the team page shows the summary line and the event block;
Figures omits offseason matches; a regular 2026 event is unchanged.

The `2025cc` blob-parity check from the previous spec's §8 retires here. It
asserted that offseason output never changes, which this design deliberately
contradicts. The season-EPA invariant above replaces it.

## 9. Rollout

1. PR branch off `cph-staging` on the fork.
2. Run the harness against the real model. **If sandbox does not beat frozen,
   stop and reopen the design** — the §2 evidence justifies building the
   harness, not merging the feature.
3. Merge the PR, then `make ship` per
   [DEPLOY.md](../rig/deploy/DEPLOY.md).
4. Trigger a full recompute so 2025 and 2026 offseason events republish.
5. Work §8's live checks and record the evidence.

## 10. Out of scope

- Preseason (type 100) and week-0 events.
- Offseason events before 2025, which the pipeline does not ingest.
- Any upstream submission. This builds on the fork-only offseason restore,
  which already reverses a maintainer decision.
- RP prediction quality at offseason events. The harness will report it; tuning
  it is separate work.
