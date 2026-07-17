# Bucket-first serving: versioned, atomic, edge-cacheable blobs

## Problem

Team and event pages mix static GCS blob reads with live API calls. When the database
layer went down (~2026-06-12, #414: every DB-backed route returns `500 {}`), the blob-backed
views kept working and the API-backed views broke. On healthy days, the API-backed views
queue on the two F1 instances during event weekends. Separately, bucket fetches append
`?t=${Date.now()/1000/60}` — not floored, so the query string is unique per request and
defeats GCS/browser caching entirely — and blob uploads are sequential and non-atomic, so a
reader mid-cycle can get a mix of old and new blobs.

This PR serves every team-page and event-page view from blobs with the API as fallback
only, and makes the blob set atomic, immutable, and edge-cacheable. It follows the
direction the outage validated: the blob layer is the resilient half, so lean on it.

## What changed and why

**Manifest-last atomic publishing** (`src/google/storage.py`, `src/google/publish.py`).
Uploading N blobs sequentially can never be atomic by itself: a crash or a concurrent
reader mid-cycle observes a torn set. Instead, each cycle uploads changed blobs to *new*
keys and then writes `manifest.json` (logical path → versioned key) as the final step.
Readers resolve every blob URL through the manifest, so they see the complete old set or
the complete new set, never a mix; a crash before the manifest write simply leaves the old
manifest pointing at the old set, and the next successful cycle recovers.

**Content addressing + copy-on-write.** Versioned keys are `v2/{path}.{sha256[:12]}` of the
compressed payload, served with `Cache-Control: public, max-age=31536000, immutable`. A blob
is uploaded only when its content hash differs from what the previous manifest referenced,
so unchanged blobs keep a stable URL — edge caches keep absorbing event-weekend load — and
per-cycle upload volume is proportional to what actually changed. This replaces the lossy
`str(Event)` upload gate, which compared hand-picked fields and let event blobs go stale for
weeks during registration windows (EPAs frozen at the last registration change). The
manifest itself is the only short-TTL fetch (`max-age=60`).

**Per-team `team/{num}` blobs.** The team page's `/team/{num}` fetch was API-only, so it
died with the database and queued on F1 during peaks. Each cycle now renders a blob per
active team (team info + all-years history) from the same `_read_team` helper the route
uses, gated by the content hash so only teams whose payload changed get re-uploaded.

**Historical blobs at `hist/{epoch}/{path}`** (`backfill_blobs.py`). Historical payloads
never change after backfill, so content-addressing buys nothing there — and listing ~100K
per-team-year objects in the manifest would make the short-TTL fetch megabytes. Historical
blobs instead use a deterministic epoch-prefixed path; the epoch is a single field in the
manifest, so one manifest fetch resolves both current and historical URLs. The backfill
script is idempotent (skips existing objects), resumable (bucket-side progress checkpoint),
uses the same `_read_*` helpers as the live routes, and skips 2021 (no season).

**Frontend bucket-first + stale-if-error** (`frontend/src/api/`). `storage.tsx` fetches the
manifest (once per 60 s, deduped in-flight) and resolves immutable URLs with no cache-buster.
`getTeam`, `getYearTeamYears` (all years), and historical `getTeamYear` become bucket-first;
the two team-match figure fetches now read the event blob and the team-year payload (which
already contain the data) instead of API-only endpoints. When both bucket and API fail,
expired IndexedDB entries are served as a last resort instead of being deleted — a transient
outage renders slightly stale data rather than an empty page. The `DISABLE_GCS` kill-switch
is honored throughout.

**Deploy-ordering compatibility.** Vercel deploys the frontend on merge; App Engine deploys
manually and may lag in either direction. The new frontend falls back to today's legacy
path + `?t=` behavior when no manifest exists (old backend), and the new backend keeps
writing the legacy unversioned paths every cycle (gated by the same hash) so the deployed
frontend keeps working unmodified (new backend). Both orders verified below.

## Measured (local rig: full 2026 season — 3,724 teams, 215 events, 18,372 matches — CockroachDB + fake-gcs-server)

| Scenario | Objects uploaded | Bytes |
|---|---|---|
| First publish (no prior manifest) | 3,950 versioned + 3,950 legacy | 24.2 MB (×2) |
| Steady-state cycle, no data change | 0 | 0 |
| One team renamed | 2 (`team/{num}`, `teams/all`) | 41.7 KB |
| Historical backfill, one full season | 3,941 | ~24 MB |

- Manifest: 169 KB at 3,950 entries, `max-age=60`. Write Storage step: 27.5 s first
  publish, 5.8–8.2 s steady state (previously 2–18 s uploading unconditionally).
- Torn-set drill: publisher killed between blob uploads and manifest write → manifest
  unchanged, 32/32 sampled referenced blobs still downloadable, readers saw the old
  name; next cycle republished and readers flipped to the new set.
- API-down drill (all backend servers stopped, bucket up): event page, team page, and
  the EPA-over-time figure fully rendered in a real browser from blobs — 24 bucket
  fetches, 0 API fallbacks, 0 `?t=` busters, exactly 1 manifest fetch.
- Blob payloads byte-identical to the corresponding site API responses (`team/{num}`,
  `team_years/{year}`, `team/{num}/{year}`). The `event/{key}` payload is equal as a set;
  ordering of teams within a match differs between pipeline- and DB-rendered
  `team_matches` — a pre-existing property at the base commit, unchanged by this PR.
- Refactored `/team/{num}` and `/team/{num}/{year}` routes verified byte-identical
  before/after the helper extraction.

## Operational notes

- **Edge caching and event locality:** immutable `v2/{hash}` URLs make caching work at
  every layer — browser, GCS edge, or any CDN placed in front of the bucket. FRC traffic
  is unusually cache-friendly: an event's audience is physically co-located (same venue,
  same CDN point of presence) and fetches the same blobs, so the first spectator warms
  the cache and origin traffic per event collapses to roughly changed-blobs-per-cycle,
  independent of crowd size. Today's `?t=` cache-buster defeats all of this — every page
  view is a full origin fetch. With this change, event-day load scales with matches
  played, not with spectators.
- **Cleanup of superseded `v2/` objects:** a naive age-based lifecycle rule would be
  incorrect — an unchanged blob keeps its hash and stays referenced by the manifest
  indefinitely, so "old" does not mean "unreferenced". A reference-aware GC job (keep
  everything the current manifest references, plus a grace window for in-flight
  publishes and cached manifests, delete the rest) is provided as a stacked follow-up
  PR. Growth without it is modest (roughly a few hundred MB per active competition day).
- **Backfill:** run `python backfill_blobs.py` once after deploy; re-runs are safe and
  resume where they left off. Bump `HIST_EPOCH` only to force a full re-export.
- The legacy unversioned writes can be retired in a follow-up once the manifest-aware
  frontend has been deployed for a while.

## Testing

Pytest coverage for the pure publish logic (hashing, manifest round-trip, copy-on-write
planning) is in a separate follow-up branch (`bucket-first-serving-tests`) to keep this PR
free of new test infrastructure. `yarn lint` clean; `yarn build` compiles (the static-export
step fails identically on the unmodified base commit — unrelated). End-to-end behavior
verified on a local rig seeded with the full 2026 season via TBA, running CockroachDB and
fake-gcs-server, including a 10-check smoke suite (9/9 static checks pass; the 10th asserts
the old "re-upload `team_years` every cycle even if unchanged" behavior, which copy-on-write
intentionally removes — the manifest cycle stamp confirms each publish ran).

## Note for reviewers

This branch and the parallel EPA-consistency branch both replace the publish gate in
`src/google/storage.py`; a rebase conflict there is expected and mechanical (this PR's
content-hash plan subsumes that branch's blob gate).
