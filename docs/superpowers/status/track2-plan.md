# Track 2 — Bucket-first serving: implementation plan

Baseline: `a2cea55`. Worktree: `.worktrees/bucket-first-serving`, branch `bucket-first-serving`.

## Re-inventory of the fetch surface at `a2cea55` (spec §1.7 asked for this)

Verified against code (not the spec's prose, which predates the re-add):

1. **`_read_event` (event/{key} blob) DOES include `team_matches`.** They are derived from
   `Match.pre_epas` / `Match.epas` via `_build_team_matches_from_matches`
   (`backend/src/site/event.py:56-99`), not from the removed `TeamMatch` pipeline entity.
   Spec §1.7's "no longer takes team_matches" is outdated at `a2cea55` — the event page's
   per-team match data is already in the event blob. The frontend `getTeamYear` current-year
   reconstruction already reads `event.team_matches`.
2. **`TeamYear.team_matches` JSON column is still populated** (`backend/src/data/epa/agg.py:74`,
   `compact_from_match`). The two "API-only" endpoints `/team_year/{year}/{team}/matches` and
   `/event/{event}/team_matches/{team}` still return real data (identical shape to the event
   blob's team_matches; endpoint adds `"team"`).
3. **Live frontend fetch surface** (callers outside `src/api/`):
   | fetch | endpoint | today | callers | decision |
   |---|---|---|---|---|
   | `getTeam` | `/team/{num}` | API-only (`checkBucket=false`) | compare/multiYear, team/summaryTabs | **new blob `team/{num}`** |
   | `getTeamYears` | `/team/{num}/years` | API-only | **none (dead)** | skip |
   | `getTeamYear` | `/team/{num}/{year}` | curr-year reconstructed from blobs; else API-only | team/main | historical → blob via backfill; curr-year keep reconstruction |
   | `getYearTeamYears` | `/team_years/{year}` | bucket only when `year===CURR_YEAR` | teams list, compare | historical → blob via backfill |
   | `getEvent` | `/event/{key}` | bucket-first already | event page | already covered |
   | `getTeamEventTeamMatches` | `/event/{event}/team_matches/{team}` | API-only | EventLine figure | **fold into event blob** (already contains it) |
   | `getTeamYearTeamMatches` | `/team_year/{year}/{team}/matches` | API-only | YearLine figure | **fold into `getTeamYear`** (team_matches present there for all years) |

## Blob coverage decision (§3.2.A/B)

- **New per-cycle blob:** `team/{num}` (site `/team/{num}` payload), gated by content hash so
  only teams whose payload changed are re-uploaded.
- **Skip** `team/{num}/years` — no live caller.
- **Do not mint `team/{num}/{CURR_YEAR}` per cycle** — keep the existing client-side reconstruction
  (avoids thousands of tiny per-cycle objects). Historical `team/{num}/{year}` comes from backfill.
- **Fold both team-match figure fetches into existing blobs** rather than minting per-team-match
  objects: EventLine reads the event blob's `team_matches` filtered by team; YearLine reads the
  team-year payload's `team_matches` (via `getTeamYear`, which already has them for all years).
- Extract `_read_team` and `_read_team_year` shaping helpers from the inline route bodies so the
  route, the per-cycle export, and the backfill all produce byte-identical payloads (§3.2 "reuse
  the `_read_*` helpers").

## Backfill script (§3.2.B)

`backend/backfill_blobs.py` (standalone, maintainer-run, imports `src`). For each past year
(2002..CURR_YEAR-1, **skip 2021**): `team_years/{year}`, `events/{year}`, `event/{key}` per event,
`team/{num}/{year}` per team. Idempotent + resumable via a bucket-side `backfill/progress.json`
checkpoint plus content-addressed skip (unchanged content → no re-upload). Writes versioned blobs
and, at the end of each year, refreshes the manifest so historical blobs are resolvable.

## Versioned atomic publishing (§3.2.C)

Two scopes, so the short-TTL manifest stays bounded (a single manifest listing every historical
`team/{num}/{year}` — ~100k objects — would be megabytes fetched every 60s):

**Volatile current-year set → content-addressed via `manifest.json`.**
- Versioned object key: `v2/{logical_path}.{hash12}` (e.g. `v2/event/2026casf.a1b2c3d4e5f6`),
  `hash12` = first 12 hex chars of sha256 of the compressed bytes. No `?`/`&` in the key → immutable,
  needs no query string.
- **`manifest.json`** written **last**, maps `logical_path -> versioned_key` for the current-year
  coherent set (teams/all, team_years/{CURR}(+variant), events/all, events/{CURR}, current
  `event/{key}`, team_to_events, noteworthy, upcoming, and the new `team/{num}` for active teams).
  Short `Cache-Control: public, max-age=60`. Readers resolve through it → coherent set; a crash mid
  upload leaves the old manifest pointing at the old set.
- Versioned blobs: `Cache-Control: public, max-age=31536000, immutable`.
- **Copy-on-write / content gate:** read prev manifest at cycle start; for each rendered blob
  compute the hash; equal to the prior version's hash → skip upload (object already at that key).
  Same comparison is the content gate for the new `team/{num}` blobs; generalizes Track 1's gate
  (expected `storage.py` conflict, noted in PR description).

**Immutable historical set → epoch-prefixed deterministic path, no per-blob manifest entry.**
- Historical blobs (`team_years/{pastYear}`, `events/{pastYear}`, `event/{pastKey}`,
  `team/{num}/{pastYear}`) never change after backfill, so content-addressing buys nothing and a
  100k-entry manifest is unacceptable. Write them at `hist/{HIST_EPOCH}/{logical_path}`,
  `Cache-Control: immutable, max-age=1yr`. `HIST_EPOCH` (small int) is published as a field in
  `manifest.json` (`hist_epoch`), so the frontend needs only the one manifest fetch to build both
  current and historical URLs deterministically. A re-backfill bumps `HIST_EPOCH`.

**Back-compat.** Legacy unversioned paths (`event/{key}`, `team_years/{CURR}`, …) are still written
each cycle (gated by the same hash) so an *old frontend* keeps working against a new backend. When
`manifest.json` is absent (old backend) the new frontend falls back to legacy path + `?t=`.

Lifecycle-rule recommendation (delete `v2/` objects unreferenced for a few days; `hist/` kept)
documented in the PR description, not code.

`manifest.json` shape: `{schema, cycle, hist_epoch, blobs: {logical_path -> versioned_key}}`.

Pure, unit-tested logic lives in `backend/src/google/publish.py`:
`content_hash`, `versioned_key`, `Manifest` (parse/serialize), `plan_uploads` (copy-on-write +
gate). GCS I/O stays in `storage.py`; tests need no GCS.

## Frontend (§3.2.D)

- `storage.tsx`: fetch `manifest.json` (short TTL, in-memory + IndexedDB cached). When present,
  resolve `logical_path -> versioned URL` and fetch it with **no `?t=` buster** and long cache.
  When absent/failed (old backend), fall back to today's legacy path + `?t=` behavior.
- Bucket-first for `getTeam`, `getYearTeamYears` (all years), `getTeamYear` (historical), plus the
  two folded figure fetches. Keep `DISABLE_GCS` kill-switch.
- **Stale-if-error:** `getWithExpiry` returns the expired entry as a last resort instead of
  deleting it when both bucket and API fail.
- Keep the current-year reconstruction (new blobs don't fully cover it).

## Deploy-ordering matrix

- new frontend + old backend: no `manifest.json` → 404 → legacy `?t=` path (unchanged behavior).
- old frontend + new backend: legacy paths still written → unchanged behavior.

## Tests

`backend/tests/` + `backend/requirements-dev.txt` (pytest only). Cover `publish.py` pure logic:
hashing determinism, versioned key format, manifest round-trip, `plan_uploads` copy-on-write &
gate. Frontend: `yarn lint` + `yarn build` must pass.
