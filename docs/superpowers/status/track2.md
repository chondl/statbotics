# Track 2 status — COMPLETE (rig-verified)

All §3.4 acceptance criteria verified against the shared rig. Review feedback applied
(tests split to separate branch/PR, comments stripped, in-flight manifest dedupe added,
history squashed to 4 commits, force-pushed).

## Acceptance evidence
- §3.4.1 API-down render: real browser (Chrome) with all backend servers stopped —
  event page, team page, EPA-over-time figure fully rendered from blobs. 24 bucket
  fetches, 0 API fallbacks, 0 ?t= busters, 1 manifest fetch. Historical-year pages not
  exercisable (rig has no past seasons); hist mechanism verified via
  hist/1/team/254/2026 == API payload and URL-resolution logic.
- §3.4.2 torn-set: write_manifest patched to raise after blob uploads → manifest
  unchanged, 32/32 sampled referenced blobs downloadable, readers saw old content;
  next cycle recovered.
- §3.4.3 headers: manifest max-age=60; v2/ + hist/ blobs max-age=31536000 immutable;
  no query strings on manifest-resolved URLs.
- §3.4.4 deploy matrix: manifest-404 fallback to legacy+?t= verified pre-publish (new
  FE + old BE); 9/9 legacy paths fetchable with ?t= and fresh post-publish (old FE +
  new BE).
- §3.4.5 payload identity: team/{num}, team_years/{year}, team/{num}/{year} byte-equal
  to API; event/{key} equal as set (team_matches intra-match ordering differs
  pipeline-vs-DB — pre-existing at a2cea55; per-team sequences identical).
- §3.4.6 volume: first publish 3,950 objects/24.2MB (+legacy copies); steady state 0;
  single-team change 2 objects/41.7KB; backfill of one full season 3,941 objects,
  40.7s, idempotent re-run 0. Manifest 169KB @ 3,950 entries.
- Smoke: 9/9 static; --run-update 9/10 (check 10 assumes the old unconditional
  team_years re-upload; copy-on-write skips unchanged content by design — manifest
  cycle stamp proved the publish ran).

## Branches / PRs (fork only)
- bucket-first-serving @ e7f8a83 (4 commits, force-pushed) — PR #2 (draft), body updated.
- bucket-first-serving-tests @ 320295f — PR #3 (draft, base bucket-first-serving), 9 pytest pass.

## Rig
Left running rig-worktree code on 8000/8001 (restored via start.sh). DB mutation
(team 254 rename) reverted; bucket has additive v2/, hist/1/, manifest.json,
backfill/progress.json objects.
