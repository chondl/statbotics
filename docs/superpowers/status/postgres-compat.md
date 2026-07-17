# postgres-compat status — COMPLETE (verified end-to-end on Postgres)

Goal: backend runs on plain PostgreSQL instead of CockroachDB (staging Cloud SQL).
Compatibility branch, NOT upstream Track 1/2.

Worktree: /Users/chondl/learn/statbotics/.worktrees/postgres-compat (branch postgres-compat, from master a2cea55)
Pushed to `fork` only (git@github.com:chondl/statbotics.git). No PR created.

## CRDB surface inventory (found)
- `src/constants.py`: CONN_STR hardcoded `cockroachdb://` scheme (prod + local dev).
- `run_transaction` from `sqlalchemy_cockroachdb` imported in 13 files
  (src/db/write/template.py, src/db/read/{event,team_year,team,match,etag,year,team_event}.py,
   src/db/functions/{noteworthy_matches,clear_year,update_teams,remove_teams_no_events,upcoming_matches}.py).
- requirements.txt / pyproject.toml: `sqlalchemy-cockroachdb`, `psycopg2` (psycopg2 already present).
- NO CRDB-specific raw SQL (no AS OF SYSTEM TIME / INT8 / SERIAL). The only text() SQL is
  plain `ORDER BY {metric} DESC/ASC` in upcoming_matches.py — dialect-agnostic.
- Upsert already uses `sqlalchemy.dialects.postgresql.insert` + on_conflict_do_update -> Postgres-native.

## Changes (2 commits on postgres-compat)
1. a48c23e — constants.py: `CONN_STR = os.getenv("DATABASE_URL") or (<existing CRDB logic>)`.
   Set DATABASE_URL=postgresql+psycopg2://... to run on Postgres; PROD/local CRDB paths untouched.
2. f8d8026 — new src/db/transaction.py: `run_transaction` wrapper dispatching on `engine.dialect.name`:
   - cockroachdb  -> delegates to sqlalchemy_cockroachdb.run_transaction (lazy import; prod unchanged).
   - else (postgresql) -> plain SQLAlchemy `session.begin()` txn with retry on SQLSTATE 40001.
   Repointed all 13 imports to `from src.db.transaction import run_transaction`.

Format/lint: black, isort, flake8 all clean on touched files.

## Verification (local, real TBA key, docker Postgres 15 on :5433, fake-gcs :4443)
- Env: DATABASE_URL=postgresql+psycopg2://postgres:***@localhost:5433/statbotics3, dialect resolved = postgresql.
- Schema: Base.metadata.create_all -> 7 tables created (teams, years, team_years, events, team_events, matches, etags).
- Full current-year build (update_curr_year(partial=False)) succeeded in 179.7s:
  2026 TBA 2m36s, EPA 4.1s, Write DB 14.9s, Write Storage 3.7s, Update DB 0.4s (upsert path OK).
- Row counts: teams 3724, team_years 3724, events 215, team_events 8304, matches 18372.
- fake-gcs: 227 blobs written (events/*, event/{key}, teams/all, team_years/{year}, etc.).
- API smoke (uvicorn :8010), all HTTP 200 with sane payloads:
  /v3/team/254 (EPA norm 1946, winrate 0.9155), /v3/year/2026, /v3/team_year/254/2026 (EPA 328),
  /v3/site/team_years/2026, /v3/site/events/2026, /v3/site/team/254 (rank 3).
- run_transaction read path on Postgres confirmed (count = 3724). Both read (endpoints) and write
  (build) paths exercise the plain-Postgres transaction wrapper.

## Behavioral caveats
- Retry semantics: CRDB path unchanged. Postgres path retries only on SQLSTATE 40001
  (serialization failure), default 3 retries. With Postgres default READ COMMITTED isolation,
  40001 is rare, so retries seldom trigger — the single-writer ETL pipeline makes contention a
  non-issue anyway. No exponential backoff on the Postgres path (immediate retry) — acceptable
  given single-writer; can add if a Postgres serializable setup ever needs it.
- ON CONFLICT upsert: identical SQL (postgresql dialect insert) runs natively on Postgres;
  verified by the build's Update DB step and full re-write of 3724 team_years. No CRDB-specific
  upsert behavior relied upon.
- sqlalchemy-cockroachdb dependency retained (needed for the CRDB branch); staging could drop it
  since it is imported lazily only when dialect == cockroachdb.

## Pending
- Shared smoke suite (docs/superpowers/rig/smoke/, per coordinator) does NOT yet exist. When the
  rig agent lands it, run it against this Postgres stack (base URL http://localhost:8010, bucket
  site_dev_v1 on fake-gcs :4443) and append its pass/fail output here. Not blocking.

## Rig resources reused
- TBA auth: hardcoded AUTH_KEY in src/tba/constants.py (== frontend public key) works; for the
  verification build used the real key at /Users/chondl/thebluealliance_api_key.txt via a
  runtime session-header override in a scratchpad seed script (NOT committed; branch does not
  touch TBA). fake-gcs + rig.env pattern reused (DATABASE_URL added).
</content>

## Shared smoke suite result (pending step DONE)

Ran docs/superpowers/rig/smoke/smoke.py against the Postgres-backed stack. To avoid colliding
with the rig's shared fake-gcs on :4443 (--run-update advances blob generations), stood up a
dedicated emulator `fake-gcs-pg` on :4453 (bucket site_dev_v1 created via API), repopulated it
with a full rebuild from Postgres (174.0s), and served this worktree's code on :8010 (api/site)
and :8011 (data). Partial cycles on the data server complete in ~11s.

Command:
    python3 docs/superpowers/rig/smoke/smoke.py \
      --base-url http://127.0.0.1:8010 --data-url http://127.0.0.1:8011 \
      --gcs http://localhost:4453 --bucket site_dev_v1 --year 2026 --run-update

Output:
    [1] liveness            PASS GET /  |  PASS GET /info 200
    [2] db-backed reads     PASS /v3/team/254 (254 norm=1946.0)  |  PASS team_years/2026 (3724)
    [3] blob reads          PASS teams/all (3724)  |  PASS team_years/2026 (3724)  |  PASS event/2026tuis (32 team_events)
    [4] consistency probe   PASS event blob epa == API epa (25 teams, max diff 0.0000)
                            PASS team_years blob epa == API epa (50 teams, max diff 0.0000)
    [5] update trigger      PASS update cycle advances team_years blob (generation 1783658629116271 -> 1783658651026626)
    OK: 10/10 checks passed (exit 0)

Note: the earlier verification build (before the dedicated emulator existed) wrote its 227 blobs
into the rig's shared fake-gcs :4443 bucket site_dev_v1 via rig.env — same 2026 content the rig
itself publishes, so harmless, but rig agents can re-run a cycle to republish if byte-exactness
vs the CRDB build matters.

My stack (left running for reuse): pg-compat (Postgres 15, :5433), fake-gcs-pg (:4453).
Smoke servers on :8010/:8011 stopped after the run.
