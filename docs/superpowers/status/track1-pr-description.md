# Fix EPA consistency: honest write/publish gates, breakdown-race deferral

Every EPA a user can see should agree across the event page, team page, teams list, and public API after each update cycle. Today three defects break that, all verified against `master` (`a2cea55`) and reproduced end-to-end on a local rig (CockroachDB + fake-gcs + full 2026 season, 3724 teams / 215 events / 18372 matches).

## 1. Event pages serve weeks-stale EPAs (blob publish gate)

**Symptom:** During an event's registration window, the event page's team list shows EPAs frozen at the last registration change while team pages and the teams list are current. Reported on ChiefDelphi (2026-04-12, therekrab, with screenshots: team 254 at `2026cancmp` — 311.5 on the event page vs 360.8 everywhere else).

**Cause:** the `event/{key}` blob — the event page's data source — is re-uploaded only when `str(event)` changes (`src/google/storage.py:80`), and `Event.__str__` (`src/db/models/event.py:88`) contains key, status, num_teams, current_match, qual_matches — no EPA fields. Every cycle recomputes all team EPAs in memory and refreshes them in the DB, but nothing that changes `Event.__str__` happens between registration changes, so the blob never republishes.

**Change:** gate the upload on the content of what the blob renders — the year, event, match, and team_event rows that `_read_event` serializes — using attrs full-field equality against the cycle-start DB state. The blob republishes exactly when its content would change.

**Verified on the rig:** planted a stale `event/{key}` blob (EPAs zeroed) with drifted team_event rows in the DB. A baseline cycle healed the DB but left the blob frozen at zeros (the reported bug); a branch cycle republished it, and every team's EPA in the event blob equaled the `team_years/{year}` blob exactly. A registered-teams/no-matches fixture event also converged: event-blob EPA == team_years-blob EPA for all teams after one cycle. On a no-change cycle the gate publishes zero event blobs (same as baseline).

**Deploy note:** the gate compares against DB state at cycle start, so it cannot retroactively fix blobs that are already stale. Run one `partial=False` cycle (`reset_curr_year`) after deploying to resync current-year event blobs once.

## 2. Public API disagrees with the website (#413, DB write gate)

**Symptom:** rank / percentile / normalized EPA differ between the REST API (reads the DB) and the site (reads blobs rebuilt from memory each cycle). Upstream issue avgupta456#413.

**Cause:** the partial-update write filter (`changed()` in `src/data/utils.py:57`) drops a row unless `str(obj)` changed, and the `__str__` methods are hand-picked subsets: `TeamYear.__str__` omits rank, percentile, and norm_epa; `TeamEvent.__str__` omits component EPAs; `Match.__str__` omits most breakdown fields. Drift in the omitted fields never reaches the DB.

**Change:** compare every column with the attrs-generated equality instead of `__str__`. `__str__` remains for logging.

**Verified on the rig:** perturbed `norm_epa` on 50 team_years rows, then ran one cycle per code version. Baseline: 50/50 rows still wrong afterwards. Branch: 0/50 — all healed. The first branch cycle on the long-running rig DB also wrote a one-time catch-up of 529 team_years rows of accumulated silent drift.

**Cost (measured):** the full-field comparison over all ~30K objects adds ~0.1 s (attrs `__eq__`; `Write DB` step 0.07 s → 0.3–0.6 s including the healed rows). Total partial-cycle time is unchanged: 13.0–13.3 s baseline vs 13.1–13.2 s branch on identical data. Steady-state row writes converge to the same ~0 as baseline on no-change cycles; extra rows appear only when real drift exists (bounded by actual drift, e.g. the 50 perturbed rows).

## 3. Component EPAs transiently crater mid-event (score-before-breakdown race)

**Symptom:** right after a match, a team's component EPAs collapse, then correct on a later cycle (ChiefDelphi reports: Raysine 2026-04-12, Barnav 2026-05-02, Isaac-The-Pro 2026-05-01).

**Cause:** TBA sometimes posts the score before the score breakdown. `get_event_matches` (`src/tba/read_tba.py`) marks a match completed as soon as both scores are >= 0, and `clean_breakdown` returns an all-zero breakdown when `score_breakdown` is null — so the EPA update consumes imputed zeros.

**Change:** for 2016+ (breakdown-bearing years), a match with a final score but no red/blue breakdown is treated as still upcoming — predictions publish as before, but the season replay does not consume it — until the breakdown arrives or the match is 24 hours old. The age fallback guarantees no match is ever stuck, including events that never post breakdowns.

**Verified on the rig** by intercepting the TBA response for a real match (`2026caasv_qm10`, 161–64) and nulling its `score_breakdown`:
- Branch: match held as `Upcoming`, predictions present; the six teams' EPAs were identical (to the 0.005 pt comparison threshold) to a replay with the match absent — nothing consumed.
- Baseline, same scenario: match ingested `Completed` with zero components; team EPAs cratered, e.g. 11096: 26.4 → 13.6, 7607: 41.2 → 28.4, 2543: 44.1 → 33.9.
- Breakdown "arrives" (unmodified refetch): match processed once with correct components; all six teams' EPAs and the match row returned exactly to their original values.
- Fallback: same missing-breakdown match older than 24 h is processed (`Completed`) — no stuck matches.

## 4. Upsert batches exceed CockroachDB's message limit

Surfaced while measuring item 2: a 1000-row team_years upsert batch renders to ~15 MiB on average (69 KB max row — rows carry per-match JSON), and a full team_years write fails on a default-configured CockroachDB with `ProtocolViolation: message size 18 MiB bigger than maximum allowed message size 16 MiB` (`sql.conn.max_read_buffer_message_size`). This can already bite today on full rebuilds; honest write gating makes large partial batches routine. `CUTOFF` drops 1000 → 250 (~4 MiB average batch). Verified at the default 16 MiB limit: baseline code fails, this branch succeeds. No measurable cycle-time impact (the rows go into the same single transaction introduced by the June `write_all` rework).

## Safety: EPA math untouched

The changes affect when rows and blobs publish, never what EPA computes. A full-season in-memory replay over the same DB state produces a byte-identical SHA-256 (year + all team_years, events, team_events, matches serialized) on baseline and branch:

```
910b184c1b7b20c0e5c026aab98d8d9a556b4e4c8a7895e8cd6a4a59fda87f04  (both)
```

The shared smoke suite (liveness, DB reads, blob reads, blob==API consistency probes, update-trigger publish advance) passes 10/10 with this branch serving.

## Measured cycle summary (local rig, static 2026 season)

| | baseline `a2cea55` | this branch |
|---|---|---|
| Partial cycle total | 13.0–13.3 s | 13.1–13.2 s |
| Write DB step | 0.07–0.08 s | 0.31–0.57 s |
| team_years rows/cycle (no drift) | 0 | 0 (after one-time 529-row catch-up) |
| team_years rows/cycle (50-row drift) | 0 (bug) | 50 |
| event blobs/cycle (no change) | 0 | 0 |
| Full team_years upsert at 16 MiB limit | ProtocolViolation | succeeds |

Tests for the new logic (write-gate equality, deferral policy) live on a separate branch (`epa-consistency-tests`) to keep this diff minimal, since the repository currently has no test infrastructure.
