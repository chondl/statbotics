# blob-gc — v2/ garbage collection + gzip manifest

STATUS: DONE (verified 2026-07-10). Untracked status doc — never committed to a
code branch.

Two changes stacked on `bucket-first-serving`, one commit each, backend only.

- Branch `blob-gc` (fork only): base `fork/bucket-first-serving` @ `032db39`.
  - `bbd17b2` Add reference-aware GC for versioned blobs
  - `0c4388f` Upload manifest.json with gzip content-encoding
- Draft PR (fork only): https://github.com/chondl/statbotics/pull/7
  (base `bucket-first-serving`, head `blob-gc`).
- Merged into `staging` as `98a15b1`, pushed to `fork` only.

`origin` never pushed. No PRs/issues/comments on `avgupta456/statbotics`. All
`gcloud` in project `statbotics-staging`. No secrets printed.

## Task 1 — reference-aware GC

`gc_versioned_blobs(grace_hours=48, dry_run=False)` in `backend/src/google/storage.py`,
exposed as `GET /v3/data/gc_blobs` (params `grace_hours`, `dry_run`) on the data
router. Lists objects under `v2/`, keeps any that are referenced by the current
`manifest.json` OR younger than `GC_GRACE_HOURS` (48), deletes the rest in batches
of 100 (`bucket.delete_blobs`). Never lists or touches legacy unversioned paths,
`hist/`, or `manifest.json`. Logs one line: `GC v2/: scanned N, kept N, deleted N, freed N bytes`.

### Why the grace window (concurrency race)

A publish uploads new `v2/` objects *before* it writes the manifest that
references them. If GC subtracted only the current manifest's references, it could
delete a freshly uploaded object the about-to-land manifest will reference, tearing
the set. The 48h grace keeps any young object regardless of reference state, so an
in-flight publish's uploads are protected until long after its manifest lands. The
same window also protects objects referenced by a previous manifest a live client
still holds (60s client cache + CDN TTL << 48h). GC is therefore safe to run
concurrently with a publish and on any schedule; it never coordinates with the
publisher. Idempotent: re-running with no publish between deletes nothing.

## Task 2 — gzip manifest

`write_manifest()` now stores `manifest.json` gzip-compressed with
`content_encoding='gzip'`. GCS transcoding serves it compressed to clients sending
`Accept-Encoding: gzip` (all browsers) and transparently decompressed otherwise —
no reader change. Frontend `fetch(...).json()` and backend `download_as_bytes()`
both get plain JSON unchanged.

## Rig verification (fake-gcs, full 2026 set)

- gzip: plain 169,138 -> gzip 53,569 bytes (0.317x). Client round-trip equal.
  `curl` through fake-gcs: with `Accept-Encoding: gzip` -> `Content-Encoding: gzip`,
  53,569 bytes, magic `1f8b`; without -> no `Content-Encoding`, 169,138 bytes,
  body starts `{`. fake-gcs reproduces real GCS transcoding (no rig limitation).
- GC: `v2/` = 4,483 objects, 3,950 referenced. `grace=48 dry` kept all (all young,
  including a planted probe) -> deleted 0. `grace=0 dry` would delete 534 (533
  superseded + probe, 858,001 bytes). `grace=0 real` deleted 534; 25/25 sampled
  referenced objects survived; probe gone. Rig state restored (manifest rewritten).

## Staging run (real GCS, project statbotics-staging)

Backend rebuilt + redeployed: revision `statbotics-api-00006-7fs` (frontend
untouched). Endpoint verified live.

- Before: manifest stored `identity`, 169,138 bytes. `v2/` = 4,274 objects, 3,950
  referenced, 324 unreferenced — but ALL objects < 48h old (bucket reseeded
  recently), so `gc_blobs?dry_run=true` at the safe 48h default correctly deleted 0.
- Ran `GET /v3/data/update_curr_year` (writes the gzip manifest via new code).
  After: manifest stored gzip, **53,559 bytes** (was 169,138 -> 68% smaller).
  `curl` with `Accept-Encoding: gzip` -> `content-encoding: gzip`, 53,559 bytes,
  magic `1f8b`; without -> 169,138 bytes, body `{` (transcoded). Copy-on-write:
  the publish created 0 new objects (static 2026 data), only bumped the manifest.
- Ran `GET /v3/data/gc_blobs?grace_hours=0` to reclaim the accumulated garbage
  (safe: run right after a completed publish, no in-flight publish, negligible
  staging traffic). **scanned 4,274, kept 3,950, deleted 324, freed 26,989,396
  bytes.** Second run idempotent: scanned 3,950, deleted 0. Manifest still resolves
  3,950 entries; smoke 9/9; site + team/254 render (HTTP 200).
- Note: the default 48h GC is a no-op on this bucket today because every object is
  younger than 48h; the reduced-grace run above was an explicit one-time reclaim of
  the pre-existing superseded objects. The scheduled job runs at the safe default.

## Cloud Scheduler

`statbotics-gc` (project statbotics-staging, us-central1): `GET .../v3/data/gc_blobs`,
daily `30 4 * * *` UTC, default 48h grace, unauthenticated GET matching
`statbotics-update`. Test-triggered once: log line landed
(`GC v2/: scanned 3950, kept 3950, deleted 0, freed 0 bytes` — all remaining
objects referenced).

## Follow-ups / notes

- Pure-logic GC tests (keep/delete partition, referenced-set subtraction) could go
  on a stacked `blob-gc-tests` branch, matching this stack's convention of keeping
  test infra off the feature branch. Not created — deemed optional.
- On prod, unreferenced objects will age past 48h between publishes, so the daily
  job at the default grace will reclaim them without the reduced-grace override.
