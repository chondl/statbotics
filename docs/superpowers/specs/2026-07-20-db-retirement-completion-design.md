# DB retirement — completion design

**Status: draft for review (2026-07-20).** Per the standing decision in
[PERF-REPROCESS.md](../rig/PERF-REPROCESS.md), DB writes are resolved by
finishing DB retirement, not by tuning. Companion spec:
[TBA persistent cache](2026-07-20-tba-cache-design.md) — it gives ETag state
a DB-free home and makes db-less full rebuilds cheap.

## 1. Where retirement actually stands

The `db-retirement` branch is **fully landed**: all 22 of its commits have
equivalents on `cph-staging` (`git cherry` confirms). Serving (`/v3` REST +
`/v3/site` + blobs) runs on DuckDB-over-Parquet, the partial-update loop is
db-less-ready end to end (ETags ride the snapshot), and `DISABLE_DB=True`
crashes nothing — every `src.db.*` engine use is call-time-guarded. The
stale branch and its `fork/backup/db-retirement` / `fork/cph-db-retirement`
remotes should be deleted, not merged.

What remains is not a merge; it is three gaps plus operations:

### Gap A — the Team lifecycle (the blocker)

Post-processed Team fields — career `wins/losses/ties/count/winrate`,
`last_active_year`, `active`, `norm_epa` and its variants, `district` — are
persisted **only** through the DB path today:

- `post_process` mutates in-memory teams **after** `process_year` has already
  written the snapshot, Parquet, and site blobs; db-less those mutations
  reach nothing (`src/data/main.py:124-146`).
- Consequence: with `DISABLE_DB=True`, partial cycles serve Team fields
  frozen from the last DB-mode reset — and a db-less `reset_curr_year` is
  **destructive**: teams reload fresh from TBA with those fields empty, and
  the wiped state gets published to the `teams` Parquet, `teams/all`, and
  `team/{num}` blobs. (`active` drives the teams page; `norm_epa` drives
  `/v3/team/{num}` — the smoke suite's `norm_epa.current is not None` check
  is a live canary for exactly this.)
- `refresh_teams` (TBA field re-sync, placeholder-name fixes, win-record
  aggregation) silently no-ops db-less; `post_process_tba`
  (`remove_teams_with_no_events`, `update_team_districts`) is skipped —
  district derivation and no-event pruning have no db-less equivalent.

### Gap B — historical tooling

`backfill_parquet.py`, `backfill_blobs.py`, and the `reprocess-year` job
driver (`clear_year` + `backfill_blobs`) read the DB by design. There is no
db-less way to regenerate historical Parquet or `hist/` blobs today; after
decommission, the GCS copies would be the only source.

### Gap C — ops surface

Cloud SQL bindings + `PGPASSWORD` secret in `Makefile` targets
(`reprocess-year`, `backfill-parquet`, `backfill-blobs`) and `deploy.sh`
(`step_backend`, `step_sql`, IAM `roles/cloudsql.client`); the
`statbotics-seed` job (runs `create_all` + `refresh_teams`); docs that
assert the DB is written each cycle (DEPLOY.md, DATA-REFRESH.md, RIG.md,
backend/CLAUDE.md, `src/data/CLAUDE.md`'s SQL seeding query, the
historical-backfill runbook).

## 2. Design

### Phase 1 — close the Team lifecycle gap (ship first, still in DB mode)

Make the pipeline the owner of Team state, GCS the store:

1. **Publish after post-process.** Move the teams-affecting publishes
   (`teams` Parquet table, `teams/all` + `team/{num}` blob rendering, the
   snapshot's `teams` list) to run **after** `post_process` on non-partial
   cycles, so post-processed fields always reach GCS. (Partial cycles don't
   run `post_process` and are unaffected.)
2. **Db-less district derivation.** Replace `update_team_districts` with an
   in-pipeline pass: `Team.district` = district of the team's latest
   TeamYear, computed from the `all_team_years` dict the pipeline already
   holds (full runs) or prior-year Parquet via DuckDB.
3. **Db-less pruning.** Replace `remove_teams_with_no_events` with a filter
   at publish time: exclude teams with no TeamEvents across years (DuckDB
   over the team_events Parquet).
4. **Db-less `refresh_teams`, automatically scheduled.** Rewrite against
   pipeline state: fetch TBA teams (`cache=False`), load current teams +
   all-history TeamYears from Parquet, compute the same staleness diff,
   republish the teams table, `teams/all`, affected `team/{num}` blobs, and
   the snapshot's teams. **Add a daily Cloud Scheduler job for it** — the
   operating principle (user decision 2026-07-21) is that no operator action
   is ever required to keep the system current with new teams, events, or
   schedules appearing on TBA. New events/schedules already flow in via the
   hourly `events/{year}` ETag check; scheduled refresh closes the team-fields
   loop (names, rookie years, placeholder fixes).
5. Delete the `Write DB` branches only in Phase 4 — until the flip, DB mode
   keeps working unchanged, which makes Phase 1 independently shippable and
   testable by diffing published blobs before/after (they must be
   byte-stable in DB mode, modulo ordering).

### Phase 2 — flip production to db-less

1. Preconditions, verified and recorded: Parquet present for 2002–2026
   (144 table objects in the manifest — already true today); Phase 1
   deployed; smoke suite labels updated (its "db-backed reads" already go
   through DuckDB in prod).
2. Flip: `gcloud run services update --update-env-vars DISABLE_DB=True`
   (note: `make ship` preserves env, so the flip is an explicit operation;
   add a `make flip-dbless` target so it's not hand-rolled). Keep the Cloud
   SQL binding in place during soak — it is inert with the flag on and is
   the rollback path.
3. Verify: full pre-deploy checklist; then a db-less `reset_curr_year` and
   one hourly partial cycle observed in logs; smoke green including the
   `norm_epa` canary; browser load of a team page and the teams list.
4. Rollback: set `DISABLE_DB=False`. The DB is stale by however long the
   flag was on; a `reset_curr_year` in DB mode rehydrates it.

### Phase 3 — db-less historical tooling

1. **`reprocess-year`** driver drops `clear_year`/`backfill_blobs`: db-less
   `process_year(year, …)` already writes the year's Parquet; add `hist/`
   blob emission from pipeline objects (today only `backfill_blobs` renders
   those), or fold historical blob regeneration into `write_parquet`.
2. **Retire `backfill_parquet.py` / `backfill_blobs.py`** once the pipeline
   can emit both artifacts db-less; the full-history story becomes
   `reset_all_years` db-less (which the TBA cache makes a ~5–8 min job).
3. Rewrite the historical-backfill runbook accordingly.
4. `src/data/CLAUDE.md` preseason seeding: replace the SQL query with the
   DuckDB-over-Parquet equivalent (same aggregates over
   `parquet/{year}/matches.parquet`).

### Phase 4 — decommission and delete (after 48 h of db-less soak; user
decision 2026-07-21)

> **Status 2026-07-27: steps 1, 2, and 4 DONE. Step 3 (code deletion) NOT
> done.** The instance, `db-password`, the `cloudsql.client` grant, the
> `statbotics-seed` DB-mode job, and three stale DB-era jobs are deleted; the
> final `pg_dump` is at `gs://statbotics-staging-db-final-export/` (private);
> Makefile/`deploy.sh`/docs are swept and `flip-db`/`flip-dbless` are gone.
> Soak was 5 d 9 h (flip 07-21T16:07Z → delete 07-27T01:2x Z). Verification
> evidence: [BILLING.md](../rig/BILLING.md#cloud-sql-decommissioned-2026-07-27--0281day).
> **Remaining:** step 3 — `src/db/read`, `src/db/write`, `src/db/functions`,
> `src/db/transaction.py`, the engine/Session in `src/db/main.py`, `CRDB_*`/
> `DATABASE_URL` constants, `root.crt`, `/info`'s `conn_str`, and the
> `psycopg2`/`sqlalchemy-cockroachdb` deps are now dead code, still shipped.

1. Final safety export: `pg_dump` to `gs://…/final-db-export/` before any
   deletion.
2. Infra: remove Cloud SQL bindings + `PGPASSWORD` from all Makefile
   targets and `deploy.sh`; delete the `statbotics-seed` job (replace with a
   documented db-less stand-up: Parquet backfill → `reset_all_years`);
   delete the `db-password` secret, the `cloudsql.client` IAM grant, and
   finally the Cloud SQL instance (billing win).
3. Code: delete `src/db/read`, `src/db/write`, `src/db/functions`,
   `src/db/transaction.py`, the engine/Session in `src/db/main.py`,
   `changed()`'s DB-diff role in `src/data/utils.py`, `CRDB_*`/
   `DATABASE_URL` constants, `root.crt`, `/info`'s `conn_str` field, and the
   `psycopg2`/`sqlalchemy-cockroachdb` deps. **Keep `src/db/models/`** —
   the attrs/ORM classes are load-bearing db-less (Parquet, snapshot, and
   DuckDB schemas all introspect them); rename the package later if the name
   grates.
4. Docs sweep: DEPLOY.md, DATA-REFRESH.md, RIG.md, backend/CLAUDE.md,
   STAGING.md. Delete the `db-retirement` branch and its two fork remotes.

## 3. Sequencing with the TBA cache spec

Either order works; the natural one is **Phase 1 → TBA cache → Phase 2+**:
Phase 1 has no dependency on the cache, and having the cache in place makes
the db-less `reset_curr_year`/`reset_all_years` used throughout Phases 2–3
fast and TBA-friendly. The cache's manifest also replaces the snapshot's
ETag element, which simplifies what the snapshot carries db-less.

## 4. Verification

- **Phase 1:** in DB mode, diff published `teams/all` + sampled `team/{num}`
  blobs and `teams` Parquet before/after — identical content. Unit tests for
  district derivation and pruning parity against the SQL versions' semantics.
- **Phase 2:** smoke suite green db-less; `norm_epa` canary after a full
  db-less `reset_curr_year`; `/info` shows no degraded-seed warning.
- **Phase 3:** `reprocess-year` on a reference year (2025) db-less; compare
  Parquet + `hist/` blobs against the DB-era artifacts (hash or
  tolerance-diff).
- Every deploy follows DEPLOY.md §2's checklist as usual.

## 5. Resolved questions

- ~~Soak length~~ **Decided 2026-07-21: 48 hours** from the production flag
  flip; the flip is instantly reversible during soak.
- ~~refresh_teams scheduling~~ **Decided 2026-07-21: automatic** — daily
  scheduled job (Phase 1 item 4). Nothing in normal operation may depend on
  an operator running anything.
- `remove_teams_with_no_events` semantics: db-less we filter at publish,
  from `teams/all` and the teams Parquet — matching today's observable
  behavior. (Spec default, unchallenged.)
