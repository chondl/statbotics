# Trailing-Year Refresh — Design Spec

**Date:** 2026-11-24
**Target:** chondl/statbotics fork → `cph-staging` → mirror (statbotics.popcornpenguins.com)
**Status:** Approved; built on `feat/trailing-year-refresh`. Amended during
the build (Tue 2026-11-24) where the code differs from the draft: §5.2
(chain transport, no-hist enforcement point, TBA freshness), §5.3 (async
`isLiveYear`), §6 and §8 (renderer input order), §7 (past-year semantics in
step 1; chain transport), §10 (new grammars). Each amendment is marked
*Amended* in place.
**Amends:** [DATA-REFRESH.md](../rig/DATA-REFRESH.md) §1, §3–§7 (refresh covers
two years during the trailing window); the pre-kickoff rollover steps and the
"hist blobs are immutable" gotcha in [backend/CLAUDE.md](../../../backend/CLAUDE.md);
[DEPLOY.md](../rig/deploy/DEPLOY.md) §3;
[historical-backfill.md](../deliverables/historical-backfill.md) (epoch wording).

## 1. Goal

The mirror rolls `CURR_YEAR` over to the next season in October, once TBA posts
the new season's events. The previous season's offseason events keep running
into December. Those events must appear and update on the mirror exactly as an
in-season event does: listed in the year's event list, the event page filling
with teams, matches and results within the current year's freshness bounds
(≤ 5 minutes while viewed, ≤ 1 hour otherwise). And once a year is past, any
later correction to it must reach its pages, not only the API.

Success is observed on the live mirror: after the build ships, `2026mmr` shows
32 teams and 40 matches and is listed for 2026; `2026mttd` and `2026nhalt`
show as completed with their matches; `2026isios` (Sun 12-06 – Tue 12-08)
fills in while it runs; after Mon 12-28 the 2026 pages render from `hist/`
with no 2026 entries left in the manifest.

## 2. The incident that motivates this

`2026mmr` (Minne Mini Regional, Sat 2026-11-21) was on TBA with 32 teams and
40 played matches. The mirror showed it as Invalid with 0 teams — in the API
(Parquet) and on the website. The 2027 rollover (PR #72, Fri 2026-11-06) caused
it, through five mechanisms, found on Tue 2026-11-24 by direct reads of TBA, the
API, the manifest, the bucket and the code:

1. **2026 stopped refreshing.** The hourly gate, the event-page ping and the
   dropped-candidate sweep (PR #65) act on `CURR_YEAR` only
   (`backend/src/data/router.py:83-97`; the ping key check at `router.py:136`;
   the frontend ping at `frontend/src/pages/event/[event_id].tsx:49`). A past
   year is refreshed only by the daily TBA sweep (`backend/src/data/sweep.py`),
   round-robin over 24 years: one visit about every 24 days. It visited 2026 on
   Tue 11-17 05:30 UTC, four days before the event.
2. **Past-year events are never live.** `get_event_status`
   (`backend/src/data/tba.py:92-101`) uses Upcoming/Ongoing only for
   `CURR_YEAR`; for any other year an event with no matches is `INVALID` and one
   with matches is `COMPLETED`. An `INVALID` event gets no team events
   (`tba.py:385`) and is dropped from `events/Y` (`backend/src/site/event.py:41`);
   a `COMPLETED` one is skipped by `check_year_partial` (`tba.py:121`). So even
   the 11-17 sweep wrote `2026mmr` as an empty Invalid event.
3. **Frozen manifest entries outrank hist blobs.** `plan_uploads`
   (`backend/src/google/publish.py:86`) carries every manifest entry forward and
   `resolveBucketUrl` (`frontend/src/api/storage.tsx:53-61`) resolves
   `manifest.blobs` before `hist/`. The last 2026 current-year cycle (Wed 11-04
   19:02 UTC) left 339 `event/2026*` entries, `events/2026`, `team_years/2026`,
   `team_years/2026.limit=100.metric=epa` and `noteworthy_matches/2026` in the
   manifest, never updated again. Observed: the served `event/2026mttd` says
   "Upcoming" with 0 matches while the API says Completed, 48 teams.
4. **Hist blobs are write-once and cached for a year.** `upload_historical`
   (`backend/src/google/storage.py:415-422`) skips existing objects and sets
   `Cache-Control: public, max-age=31536000, immutable` (observed on
   `hist/2/event/2026mmr`, a Cloudflare HIT). That empty blob can never be
   replaced under epoch 2.
5. **Search finds the event, the page is empty.** `events/all` is rebuilt every
   cycle, so search lists `2026mmr`; clicking through resolves to the empty
   hist blob. This matches the user reports.

The documented recovery (`make reprocess-year YEAR=2026`, with or without a
`HIST_EPOCH` bump) would not have fixed it: mechanism 2 writes the event empty
again until it is over, and mechanism 3 shadows any hist blob it writes.

## 3. Terms

Each is used with exactly this meaning throughout.

- **Trailing year** — `CURR_YEAR − 1`.
- **Trailing window** — from the rollover until **Dec 28 00:00
  America/Los_Angeles of the trailing year** (for the 2027 rollover: Mon
  2026-12-28 08:00 UTC). Open means "now is before that instant". Fixed by
  owner decision (Tue 2026-11-24) so the close cost lands in the slow week
  before kickoff. A code rule, not a per-year constant. Computed with
  `zoneinfo`; if the zone database is missing, the process fails at startup.
- **Live year** — `CURR_YEAR`, and the trailing year while the trailing window
  is open. The pipeline gives a live year current-year semantics (§5.1).
- **Event date window** — the existing per-event interval
  `start − 1 day … end + grace` (`in_event_window`, `EVENT_END_GRACE_DAYS`).
  Renamed in prose from "in-window" to avoid colliding with "trailing window".
- **Trailing cycle** — one `process_year` run for the trailing year (§5.2).
- **Trailing publish** — the manifest write of a trailing cycle (§6).
- **Window close** — the step that retires the trailing year (§7).
- **Year epoch** — the hist epoch that one year's pages resolve under (§8).
- **Site blobs of year Y** — the logical paths `write_hist_blobs` renders:
  `team_years/Y`, `events/Y`, `event/{key}` per event of Y, `team/{num}/Y` per
  TeamYear of Y. (The code's name, `storage.py:426`.)
- **Year keys of year Y** — every logical path whose year (by §8's year-from-path
  rule) is Y. A superset of the site blobs: also `team_years/Y.limit=…`,
  `noteworthy_matches/Y`, and Parquet keys `parquet/Y/…`.
- **Manifest precondition** — writing `manifest.json` with
  `if_generation_match` set to the generation that was read (§9).

## 4. Approaches considered

1. **The trailing year runs the current-year machinery; past years get
   per-year epochs (chosen).** During the trailing window the trailing year
   publishes versioned blobs through the manifest, like the current year.
   At window close, and on any later correction, a year's hist blobs are
   rewritten under a new year epoch.
2. **One global `HIST_EPOCH` bump at window close.** Rejected after review
   (owner, Tue 11-24): re-exports ~150k objects for every past year through a
   Parquet→render path that does not exist, too heavy for the service, and a
   wiped manifest would revert to stale blobs.
3. **Keep corrective manifest entries permanently.** Rejected: the owner wants
   the manifest clean after Dec 28.
4. **Roll over only at kickoff.** Rejected by the owner: the October rollover
   makes the new season browsable.

## 5. Refresh

### 5.1 Live-year semantics

Every `CURR_YEAR` test in `backend/src` is classified as either **live-year**
(behaviour an ongoing event needs) or **current-year-only** (behaviour that is
about the newest season), and the plan lists each one. Known live-year tests:
`get_event_status` (`tba.py:92-101`), the full-fetch decision
(`full_curr_year`, `tba.py:303`), the TBA tiering of in-window events, and the
ping key check (`router.py:136`). Current-year-only: the seed of next-season
EPA, `curr_year_gcs` publish of current-year artifacts, `write_snapshot`'s
current-year key use. A live-year test takes a `is_live_year(year)` predicate
from one module (`src/data/trailing.py`), which also owns the trailing-window
rule.

### 5.2 Trailing cycles

`GET /v3/site/update_curr_year` (hourly cron and cold pings) gains a second
probe, after the current-year probe, only while the trailing window is open:

- **Bootstrap.** If the manifest's `trailing_year` is not the trailing year
  (absent today), escalate to a **full** trailing cycle. That full cycle's
  trailing publish stamps `trailing_year`, which is the bootstrap marker. The
  2026 snapshot left over from when 2026 was current reads fine (same
  fingerprint) and must not be trusted; the marker, not the snapshot, decides.
- **Otherwise** read the trailing snapshot (`read_snapshot(trailing)`) and run
  `check_year_partial(trailing, …)`: the events-list etag (same URL as stored),
  every event-date-window non-completed event's matches/rankings/alliances, and
  the PR #65 dropped-candidate sweep with fresh probes. Escalate on change. An
  unreadable snapshot escalates to a full trailing cycle.

Escalation self-calls a new `GET /v3/data/update_trailing_year[?full=1]` as a
background task, like the current-year cycle. A trailing cycle runs
`process_year(trailing, partial, tba_partial=partial, cache=partial, …)` and
then (*Amended:* a full trailing cycle fetches TBA fresh, `cache=False`, and
loads teams with `load_teams_tba(cache=False)`; a partial one keeps the etag
cache):

- writes the trailing year's Parquet and its site blobs in **one** manifest
  write (the trailing publish, §6) — not the two separate writes past years
  make today;
- writes the trailing snapshot after the manifest (blobs/manifest first,
  snapshot last, the current year's invariant);
- writes **no** hist blobs, enforced in code (*Amended:* `write_hist_blobs`
  no longer exists; `year_epoch.publish_past_year`, the only hist writer,
  raises `ValueError("live year")` for a live year);
- chains a full current-year re-render plus `refresh_teams` **only if** the
  trailing year's `parquet/Y/team_years` digest changed. This can fire on an
  offseason weekend: a team with no season record (for example a 2027 rookie
  at a 2026 offseason event) gets a TeamYear and shifts ranks and percentiles
  (`tba.py:491-513`, `agg.py:94-101`). That is correct and accepted; it is a
  full 2027 cycle, cheap before kickoff. *Amended — transport:* from the
  service endpoint the chain is a **separate HTTP request**,
  `GET /v3/data/reset_curr_year`, sent from a daemon thread after the
  pipeline lock is released (inside the lock it would skip as busy). From
  `reprocess_year` and the Cloud Run job it runs **inline**
  (`chain="inline"`), because the job's process exits when the driver
  returns and would kill an HTTP self-call in flight.

The daily sweep skips the trailing year while the window is open
(`normalize_cursor`), so nothing else writes its hist blobs.

### 5.3 Frontend

One helper in `frontend/src/api/storage.tsx`, true for `CURR_YEAR` and for
`manifest.trailing_year`. *Amended:* it is async, `isLiveYear(year):
Promise<boolean>`, and reads the manifest itself through the module's cached
`getManifest()` (60 s TTL), so callers write `const live = await
isLiveYear(year)` and never pass a manifest. It replaces the scattered
`year === CURR_YEAR` tests that decide live behaviour:

- the event-page ping (`[event_id].tsx:49`), keeping the event date window;
- `checkBucket` and the 60 s TTL in `events.tsx:9`, `matches.tsx:39`,
  `team.tsx`, `teams.tsx`.

The backend owns the window; the frontend only reads `trailing_year` from the
manifest. No date logic in the frontend.

## 6. Trailing publish

The site-blob renderers are split out of `write_hist_blobs` into render-only
functions (no upload), `render_site_blobs`. *Amended:* inputs keep the
pipeline's natural order and are **not** sorted. Renderers that need an order
sort internally, and the rest are order-preserving, so the bytes already match
the existing `hist/2` objects and an unchanged year advances no year epoch;
sorting would have changed every year's bytes. A trailing publish renders the
trailing year's site blobs and publishes them through `plan_uploads` as
versioned `v2/{path}.{hash}` objects. Unchanged paths upload nothing.

The first trailing publish (bootstrap) also **removes** every year key of the
trailing year that it did not render (for 2026: `team_years/2026.limit=100…`,
`noteworthy_matches/2026`); those resolve to a hist 404 and fall back to the
site API, which serves correct data from Parquet.

Size: a full publish of 2026 is ~340 events plus ~3.7k team pages, so the
manifest grows from 5,384 entries (73 KB gzipped) to about 9k (~120 KB) until
Dec 28. Accepted: it is temporary, and Workers bill per request, not per byte.

## 7. Window close

On the first gate tick at or after Dec 28 00:00 PT, the gate runs window close
instead of a trailing probe, as a background self-call
`GET /v3/data/close_trailing_year`. It is **stateless**: it is needed when the
trailing window is closed and the manifest still holds any year key of the
trailing year or `trailing_year` is set. If a run dies, the next tick runs it
again.

1. A final **full** cycle (fresh TBA, no etag trust). *Amended:* it runs with
   **past-year** semantics, not live-year semantics. After close the year is
   an ordinary past year; computing it the way the sweep will later compute
   it avoids a spurious year-epoch advance on the next sweep visit. The two
   semantics differ: an event that never posted a match becomes Invalid
   (dropped from `events/Y`, no team events), and an Ongoing event with no
   elims becomes Completed. Both are upstream past-year behaviour.
2. A **year-epoch advance** for the trailing year (§8): write all its site blobs
   under the next year epoch.
3. One manifest write (with the manifest precondition) that records the new
   year epoch, removes every year key of the trailing year except its Parquet
   keys, and stamps `trailing_year: null`. *Amended:* steps 2–3 are the
   ordinary past-year path. `publish_past_year` records the year epoch and
   removes the non-Parquet year keys in one manifest write; a second write
   then stamps `trailing_year: null`. Each write is idempotent, so a crash
   between them is completed by the next run (close stays needed while
   `trailing_year` is set).
4. *Amended:* if the trailing year's `parquet/Y/team_years` digest changed,
   window close chains the current-year re-render exactly as a trailing cycle
   does (§5.2): a separate `GET /v3/data/reset_curr_year` from the service,
   inline (`chain="inline"`) from a job.

Work size is a full single-year cycle plus ~4k uploads — the same order as a
daily-sweep reprocess, which already runs in-request on the service today
(the 11-17 sweep reprocessed 2026 that way). No re-export of other years.
If timing during the build shows it exceeds the gunicorn worker timeout
(`backend/Dockerfile:22`, `-t 1200`), close runs as the Cloud Run job pattern
`make reprocess-year` uses instead; the plan measures this before choosing.

## 8. Year epochs

Hist paths stay `hist/{epoch}/{logical_path}`. The manifest gains
`hist_epochs: {"<year>": <epoch>}`. A year absent from the map resolves under
the global `hist_epoch`, which stays `HIST_EPOCH` exactly as today; the map only
holds years advanced past it.

- **Year-from-path rule** (one function per side, backend and frontend, with a
  shared table of test vectors checked by both test suites): `event/{key}` →
  the key's leading 4 digits; `events/Y…`, `team_years/Y…`,
  `noteworthy_matches/Y…`, `parquet/Y/…` → `Y`; `team/{num}/Y` → `Y`; anything
  else (`team/{num}`, `teams/all`, `events/all`, `upcoming_matches…`) has no
  year and resolves under the global `hist_epoch`.
- **`resolveBucketUrl`**: a manifest entry still wins; otherwise
  `hist/{max(hist_epochs[year] ?? 0, hist_epoch)}/{path}`. The `max` means a
  later global `HIST_EPOCH` bump (a renderer change) supersedes every per-year
  entry; the next manifest write then drops map entries at or below the global
  epoch.
- **Year-epoch advance** for year Y: render all its site blobs, upload them
  under `e' = current year epoch of Y + 1`, **overwriting** (an epoch nobody
  resolves is writable; it becomes immutable only once the manifest points at
  it), then set `hist_epochs[Y] = e'` in one manifest write. Readers switch
  atomically; a missing `hist/e'/…` object 404s uncached (observed:
  `private, max-age=0`, Cloudflare BYPASS), so a reader can never cache a gap.
  Old-epoch objects are left in place (small; not referenced).
- **Standing rule for past years.** Every past-year reprocess — the daily sweep,
  `make reprocess-year`, window close — compares the rendered site blobs with
  the stored objects of the year's current epoch (md5 of `compress` output,
  which is deterministic for the pipeline's input order — *Amended:* no
  sorting, §6; looked up per candidate path,
  not by a prefix listing, since `team/` spans every year). On any difference
  it does a year-epoch advance; otherwise it writes nothing. This replaces
  `write_hist_blobs`'s "skip existing" gap-fill for years that already have
  blobs, and is why no past year's pages can stay stale again.

`HIST_EPOCH` stays a code constant that sets the global epoch and is still the
lever for a renderer change across all years. Every place that stamps
`hist_epoch=HIST_EPOCH` (`storage.py:317,362`) carries `hist_epochs` forward
unchanged.

## 9. Manifest integrity

The manifest now holds correctness-critical state (`hist_epochs`,
`trailing_year`), and two Cloud Run instances (`deploy.sh:90`,
`MAX_INSTANCES=2`) plus the sweep, `refresh_teams` and both cycles all
read-modify-write it. Today there is no lock (DATA-REFRESH.md §6). So:

- `Manifest` (`publish.py`) gains `trailing_year` and `hist_epochs`;
  `from_json`, `to_json` and `plan_uploads` carry them, and every writer
  (`write_objs`, `publish_team_artifacts`, `write_parquet`, the new publishes)
  goes through them.
- Every manifest write uses the manifest precondition; on a 412 the writer
  re-reads, re-applies its own change to the fresh manifest, and retries
  (bounded; then fails loudly into `/info`).
- `read_manifest` distinguishes "not found" from "error". On error a writer
  aborts instead of planning from an empty manifest — otherwise one transient
  read error would erase `hist_epochs` and revert 2026 to its stale epoch-2
  blobs permanently.

## 10. New entities & identifiers

- Manifest field `trailing_year`: integer year or `null`. Written by trailing
  publishes and window close. Bootstrap marker and frontend live-year source.
- Manifest field `hist_epochs`: map of year (string) → integer epoch, each
  greater than the global `hist_epoch`. Correctness-critical (§9).
- Year-from-path grammar (§8), shared by backend and frontend through test
  vectors.
- Endpoints `GET /v3/data/update_trailing_year[?full=1]` and
  `GET /v3/data/close_trailing_year`. Like every `/v3/data/*` endpoint they are
  unauthenticated, so anyone can trigger a heavy cycle; that is true of the
  existing endpoints and is not changed here.
- Module `backend/src/data/trailing.py`: the trailing-window rule and
  `is_live_year`.

*Amended — grammars added in the build:*

- **Gate response field `trailing`.** `GET /v3/site/update_curr_year` adds
  `"trailing": "full" | "partial" | "close" | null` to every response, beside
  the unchanged current-year `status` (`skipped`, `backgrounded`, or
  `skipped` with `reason: "snapshot-unreadable"`). From
  `trailing_gate_action()`: `"close"` when `close_would_act` holds; `null`
  when the trailing window is closed otherwise; `"full"` (bootstrap) when the
  manifest's `trailing_year` is not the trailing year or the trailing
  snapshot is unreadable; `"partial"` when `check_year_partial` sees new TBA
  data; else `null`. A probe failure logs
  `TRAILING GATE: probe failed (<type>: <message>); no trailing action` and
  yields `null`. `"full"`/`"partial"` schedule
  `update_trailing_year?full=1|0`, `"close"` schedules `close_trailing_year`.
- **`close_would_act(manifest, year)`** (`src/data/main.py`): true when the
  trailing window is closed and `close_needed` holds, i.e. the manifest sets
  `trailing_year` or holds any non-Parquet year key of `year`. The gate and
  `close_trailing_year` share it.
- **Endpoint responses.** `update_trailing_year`:
  `{"status": "success", "year", "full", "chain"}`,
  `{"status": "skipped", "reason": "window-closed" | "gcs-disabled", "year"}`,
  or `{"status": "busy"}`. `close_trailing_year`:
  `{"status": "closed", "year", "chain"}`,
  `{"status": "not_needed", "year"[, "reason": "gcs-disabled"]}`, or
  `{"status": "busy"}`. `chain` is whether the trailing year's
  `parquet/Y/team_years` digest changed.
- **`publish_past_year(year, rendered)` outcomes** (`src/google/year_epoch.py`):
  `"unchanged"` (every path matches under the year epoch `e`; nothing
  uploaded), `"gap_filled"` (no path exists under `e`; all uploaded under `e`),
  `"advanced"` (some path missing or different; all uploaded under `e + 1`,
  then `hist_epochs[str(year)] = e + 1`). `e` is never below the code's
  `HIST_EPOCH`. Raises `ValueError("live year")` for a live year.
- **`trailing/status.json`** (bucket object, `Cache-Control: no-store`,
  best-effort, read by `/info` as `TRAILING_STATUS`):
  `{"year": int, "state": "running" | "ok" | "error", "full": bool,
  "started": ISO-8601 UTC, "finished": ISO-8601 UTC | null,
  "error": "<type>: <message>" | null}`. Written at the start and end of
  every trailing cycle and of every window close that acts.
- **`/info` fields:** `TRAILING_YEAR`, `TRAILING_WINDOW_OPEN`,
  `TRAILING_STATUS`, `HIST_EPOCHS` (`null` when the manifest is unreadable),
  `HIST_EPOCH_SHADOWED` (`HIST_EPOCH <= max(hist_epochs)`),
  `MANIFEST_CONFLICTS` (`update_manifest` calls that exhausted their retries).
- **Completion log lines** (stdout):
  `TRAILING {year} CYCLE COMPLETE full={0|1} secs={n}`,
  `TRAILING {year} CLOSE {not_needed|closed|error} secs={n}`,
  `YEAR EPOCH {year} {unchanged|gap_filled|advanced} epoch={e}`, and
  `PIPELINE BUSY: skipped <name>` when a pipeline finds the pipeline lock
  held (`<name>` is `update_curr_year`, `refresh_teams`, `revalidate_tba`,
  `update_trailing_year` or `close_trailing_year`).

## 11. Operator surface and docs

- **No new operator step.** The rollover runbook (backend/CLAUDE.md → Season
  Prep) loses its "`make reprocess-year YEAR=<old year>`" step: the trailing
  year bootstraps itself and gets its hist blobs at window close.
- `make info` / `/info` show the trailing year, whether the window is open,
  the last trailing cycle, `hist_epochs`, and any failed close or manifest
  precondition exhaustion.
- Docs swept in the same PR: DATA-REFRESH.md §1, §3 (gate diagram), §4, §5
  (table), §6 ("no lock" caveat replaced by §9), §7; DEPLOY.md §3; backend/CLAUDE.md
  hist-blob gotcha (manifest outranks hist; year epochs; the "sweep refills one
  year per run" claim, which is false: `sweep.py:93` reprocesses only on a TBA
  200) and Season Prep; the docstrings at `storage.py:438-443` and
  `main.py:151-157`; the `HIST_EPOCH` comment in `constants.py`;
  historical-backfill.md:83-90.

## 12. Rollout

1. Ship (`make ship`) with the pre-deploy checklist of DEPLOY.md §2.
2. Pause the cron, trigger the gate once by hand, and watch the bootstrap
   trailing cycle (`make pipeline-logs`). Resume the cron.
3. Verify on the live mirror, API and a real browser load: `2026mmr` (32
   teams, 40 matches) in `events/2026` and on its page; `2026mttd`,
   `2026nhalt` completed; a team page of a `2026mmr` attendee; `/info`.
   `make smoke`, `make verify-web`.
4. Watch `2026isios` (Sun 12-06 – Tue 12-08) fill in during the event.
5. After Mon 12-28 08:00 UTC: window close ran; `hist_epochs` has `2026: 3`;
   the manifest holds no 2026 year keys except Parquet; 2026 pages render
   from `hist/3/`; `trailing_year` is null.

## 13. Testing

- Window rule: open/closed either side of 2026-12-28 08:00 UTC, using fixed
  UTC instants.
- Live-year semantics: a trailing-year event with no matches is Upcoming, with
  some matches Ongoing, and is probed by `check_year_partial`.
- Gate: bootstrap when `trailing_year` is absent; escalation on an events-list
  etag change, a match change, and a dropped candidate filling in; no trailing
  probe after close. Ping accepts trailing-year keys only while open.
- Trailing publish: hash-gated; bootstrap removes unrendered year keys; one
  manifest write; no hist writes for a live year.
- Year epochs: year-from-path vectors pass on both sides; advance writes then
  flips; an unchanged reprocess writes nothing; resolution falls back to the
  global epoch.
- Window close: stateless re-run after a simulated crash at each step.
- Manifest integrity: a 412 retry preserves both writers' changes; a read
  error aborts the write; unknown-field round trip.

## 14. Cost

Between rollover and Dec 28: one extra events-list probe per hourly tick, plus
probes for trailing-year events inside their event date windows (at most ~80
TBA requests an hour on an event weekend with pings). A trailing cycle runs only
when TBA changed, sometimes chaining a pre-kickoff current-year cycle. After
Dec 28: nothing recurring; a past-year correction found by the sweep costs one
year-epoch advance (~4–8k uploads) instead of being silently dropped. One-time
on Mon 2026-12-28: a full 2026 cycle and ~4k uploads.

## Adversarial review (folded in)

Two independent reviewers (code-grounded; external systems and failure modes)
read the first draft on Tue 2026-11-24. Material findings and their disposition:

| Finding | Disposition |
|---|---|
| Past-year events are never Upcoming/Ongoing (`get_event_status`), so a refreshed 2026 still drops a live event | New mechanism 2 in §2; live-year semantics, §5.1 |
| The ping endpoint rejects trailing-year keys (`router.py:136`) | §5.1, §13 |
| No single-flight guard exists; 2 instances; unguarded manifest read-modify-write | §9 manifest precondition; "shared guard" claim removed |
| `Manifest` drops unknown fields, so new fields would be erased | §9 |
| Moving the global epoch into the manifest would be reset by every publish, and a wiped manifest reverts permanently | Global epoch stays a constant; per-year map carried forward; `read_manifest` fails closed, §8–§9 |
| Global re-export of ~150k objects is too heavy and needs a nonexistent loader | Owner chose per-year epochs; only the closing year is rewritten, §4, §7, §8 |
| Corrections after close would still be dropped (mechanisms 3–4 recur) | Standing rule in §8 |
| `team_years/2026.limit=100…` and `noteworthy_matches/2026` were outside "page blobs" | "Year keys" term; bootstrap and close remove them, §6–§7 |
| `checkBucket` is `CURR_YEAR`-only in `events.tsx`/`matches.tsx` | `isLiveYear` helper covers it, §5.3 |
| "Offseason updates leave team_years untouched" is false | Chain gated on the team_years Parquet digest, cost accepted, §5.2 |
| Bootstrap "full regardless" had no mechanism | `trailing_year` is the marker, §5.2 |
| No-hist-writes during the window relied on the runbook | Enforced in code: `write_hist_blobs` and the sweep skip a live year, §5.2 |
| Render bytes depend on dict order; prefix listing spans all years | Sorted inputs; per-candidate md5 lookups, §6, §8 |
| Two manifest writes per trailing cycle | Folded into one, §5.2 |
| Window close as a long HTTP task with an end-of-run marker | Stateless close; job fallback if it exceeds the worker timeout, §7 |
| Counts in §2 were wrong (339 not ~560; no `team/N/2026` entries) | Corrected |
| "in-window" collides with "trailing window"; "page blobs" duplicates "site blobs" | Terms in §3 |
| Unauthenticated heavy endpoints | Stated in §10 |

Verified and holding: TBA lists only `2026isios` after Tue 11-24 and nothing
straddles Dec 28; the earliest 2027 event is `2027mnbt` on Sat 2027-02-20; the
close instant is 08:00 UTC, clear of the 04:30/05:30/06:30 UTC daily jobs; a
missing hist object 404s uncached; `compress` output has no Content-Encoding so
GCS md5 is over the stored bytes; the sweep reprocesses only on a TBA 200.

Lens dispositions: identity — new manifest fields, endpoints and the
year-from-path grammar listed in §10; single source — the backend owns the
window, one `isLiveYear` helper, the year-from-path rule shared by test
vectors; state and caching — §9; config — `zoneinfo`, fail at startup, no new
secrets; layering — close sized against the worker timeout with a job
fallback; succession — doc and docstring sweep in §11; vocabulary — §3;
security — neutral, unauthenticated endpoints noted; proportionality — no
global re-export, no new operator step, a one-time cron pause in rollout.
