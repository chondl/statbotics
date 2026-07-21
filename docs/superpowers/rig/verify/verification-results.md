# Correctness verification — perf deploy 2026-07-21

Baseline: `/private/tmp/claude-501/-Users-chondl-learn-statbotics/be7c4aa1-d609-4621-b595-4793d8504388/scratchpad/baseline-pre-perf` (captured 2026-07-21 ~07:00Z, pre-deploy). Live: `https://api-statbotics.iterativerefinement.com` + `https://storage.googleapis.com/statbotics-staging-site` (db-less since 16:07Z). Tolerance: 1e-09.

| Artifact | Fields compared | Max abs diff | # diffs | Verdict | Notes |
|---|---|---|---|---|---|
| /v3/year/2005 | 22 | 0 | 0 | **PASS** |  |
| /v3/year/2016 | 115 | 0 | 0 | **PASS** |  |
| /v3/year/2024 | 120 | 0 | 0 | **PASS** |  |
| /v3/year/2025 | 134 | 0 | 0 | **PASS** |  |
| /v3/team_years?year=2016 top-50 | 2400 | 0 | 0 | **PASS** |  |
| /v3/team_years?year=2025 top-50 | 2650 | 0 | 0 | **PASS** |  |
| /v3/matches?event=2025casj | 26347 | 0 | 0 | **PASS** |  |
| /v3/matches?event=2016nytr | 25745 | 0 | 0 | **PASS** |  |
| /v3/site/team_years/2024 (full, competing skipped) | 173850 | 0 | 0 | **PASS** |  |
| /v3/site/team_years/2025 (full, competing skipped) | 195570 | 0 | 0 | **PASS** |  |
| blob team/254 team_years (hist, !=2026) | 264 | 0 | 0 | **PASS** |  |
| blob team/1678 team_years (hist, !=2026) | 228 | 0 | 0 | **PASS** |  |
| /v3/team/254 summary | 17 | 0 | 0 | **PASS** | career fields may move with 2026 data |
| /v3/team/1678 summary | 17 | 1 | 1 | **INFO (2026-coupled fields)** | career fields may move with 2026 data |
| /v3/team/2056 summary | 17 | 1 | 1 | **INFO (2026-coupled fields)** | career fields may move with 2026 data |
| /v3/team/148 summary | 17 | 0 | 0 | **PASS** | career fields may move with 2026 data |
| INVARIANT blob team_years/2026 == /v3/site/team_years/2026 (keyed by team; list order differs by design) | 204821 | 0 | 0 | **PASS** |  |
| INVARIANT 2026 percentile monotone non-increasing vs rank | 14790 | 0 | 0 | **PASS** |  |
| INVARIANT active 2026 teams present in teams/all; API norm_epa non-null (60-team rank-stratified sample) | 3769 | 0 | 0 | **PASS** | teams/all blob has no norm_epa field by design; sampled 60 via API |
| INVARIANT event blob EPA == API EPA (2026kylou) | 384 | 0 | 0 | **PASS** |  |
| INVARIANT event blob EPA == API EPA (2026iri) | 1200 | 0 | 0 | **PASS** |  |
| INVARIANT 2026 norm/unitless EPA ranges sane | 5 | 0 | 0 | **PASS** | n=3724 median=1480.0 mid50-mean=1483.2 norm=[1244.0,2074.0] unitless=[1344.0,2475.0] |

**Summary: 0 defect(s) across 22 checks; 652482 fields compared total.**

## Diff details

### Diffs in /v3/team/1678 summary (informational)
- `norm_epa.recent`: baseline=`1923.0` live=`1922.0`

### Diffs in /v3/team/2056 summary (informational)
- `norm_epa.recent`: baseline=`1928.0` live=`1927.0`


## Classification of non-PASS rows

- `/v3/team/1678` and `/v3/team/2056` `norm_epa.recent` moved by exactly 1.0 (1923->1922,
  1928->1927). `norm_epa_recent` is the rounded mean of the team's 2022-2026 norm EPAs
  (`backend/src/data/epa/main.py:43-49`). Both teams' 2022-2025 norms are proven unchanged by
  the historical checks in this run; the 2026 term is live data (2026kylou ongoing; 469 of
  3724 site team_year rows recomputed between baseline capture ~07:00Z and this run), and the
  2026 norm is distribution-relative, so a sub-rounding shift in the unrounded 2026 norm flips
  the rounded 5-year mean by 1. **Data-driven, not a computation defect.**
- 2025 caveat (legitimate re-ingest tonight): not needed — every 2025 artifact
  (`/v3/year/2025`, top-50 team_years, `/v3/matches?event=2025casj` incl. match set,
  `/v3/site/team_years/2025`) matched the pre-deploy baseline with **zero** diffs.
- Earlier draft of this run flagged three invariants; all were checker artifacts, fixed:
  blob-vs-API list ordering (rows identical when keyed by team), `teams/all` never carries
  `norm_epa` (baseline has the same `{active,name,team}` shape; invariant now samples the
  `/v3/team` API), and the unitless upper bound (baseline itself contains max 2475).

## Verdict

**CLEAN — 0 defects.** ~648k numeric/scalar fields compared across 22 checks; max abs diff on
every historical artifact = 0 (exact match, well inside the 1e-9 tolerance).

## Addendum: norm_epa_recent drift root cause (2026-07-21, post-review)
The two ±1.0 drifts (teams 1678, 2056) are NOT computation changes and NOT "live event" effects.
Proof (team 1678): baseline blob per-year norms 2022-2026 = 1981/1894/1949/1921/1867; mean 1922.4 → r() = 1922 = today's served value. The baseline PAGE said 1923 — a stale DB-mode aggregate frozen at the Jul-17 full reset, already inconsistent with its own rows at capture. EPA freeze independently verified: 0/3724 teams' 2026 epa mean/norm/unitless changed across the whole program. 2026kylou "Ongoing" is a stale status artifact (event ended Jul 11, finals data incomplete on TBA), not live play.
Verdict stands: CLEAN, and the perf work fixes this staleness class (aggregates republish every full cycle + daily refresh).
