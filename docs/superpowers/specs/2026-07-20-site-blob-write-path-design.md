# Site-Blob Write Path — Change Gating + orjson — Design Spec

**Date:** 2026-07-20
**Target:** chondl/statbotics fork → `cph-staging` → mirror (statbotics.iterativerefinement.com)
**Status:** Draft for review
**Related:** [PERF-REPROCESS.md](../rig/PERF-REPROCESS.md) (perf audit, §2.3),
[snapshot pickle+zstd spec](2026-07-20-snapshot-pickle-zstd-design.md),
[DB retirement completion spec](2026-07-20-db-retirement-completion-design.md),
[TBA cache spec](2026-07-20-tba-cache-design.md)

## 1. Problem

Every current-year cycle, `write_objs` (`backend/src/google/storage.py:124-271`)
re-renders and re-compresses nearly the whole site-blob set even when nothing
changed. Live 2026 counts:

| Blob group | Count/cycle | Gated today? |
|---|---|---|
| Singletons (`teams/all`, 2× `team_years/{year}`, `events/all`, `events/{year}`, `team_to_events`, `noteworthy_matches/{year}`, 4× `upcoming_matches`) | 11 | No (always rendered) |
| `event/{key}` | ~223 | **Yes** — `nan_safe_eq` vs `orig_objs` + manifest presence (storage.py:182-200) |
| `team/{num}` | ~3,724 | **No** — rendered every cycle (storage.py:208-222) |

That is ~3,735 `compress()` calls per cycle — each `json.dumps(...).encode()`
+ `zlib.compress` (storage.py:48-55) — even on a quiet cycle where zero
matches were played. Upload dedup exists only *after* compression:
`plan_uploads` content-hashes the compressed bytes and skips unchanged uploads
(`src/google/publish.py:88-93`), so the waste is render+serialize+compress
CPU, not GCS traffic. The perf audit measures the combined Write
Snapshot/Storage/Parquet step at 12–35 s per cycle
([PERF-REPROCESS.md](../rig/PERF-REPROCESS.md) §1); the ungated team-blob loop
is the bulk of the storage share. Each team blob embeds the team's
*full-history* team_years, fetched fresh from the DB every cycle
(`get_team_years_db`, storage.py:209).

User-approved decisions (2026-07-20): keep the zlib+JSON wire format; fix the
change gating; adopt orjson if it pays (this spec concludes it does, §5).

## 2. Verified current behavior

- `compress()` = `json.dumps(data).encode("utf-8")` + `zlib.compress` at the
  default level (storage.py:48-55). Frontend decodes with `pako.inflate` +
  `JSON.parse` (`frontend/src/api/storage.tsx:97-101`); the smoke suite with
  `zlib.decompress` + `json.loads` (`docs/superpowers/rig/smoke/smoke.py:82`).
- `orig_objs` flows in from `src/data/main.py:62` (`deepcopy(objs)` at cycle
  start) and is passed to `write_objs` **only on partial cycles**
  (`src/data/main.py:100`: `orig_objs if partial else None`). On full rebuilds
  the event gate degrades to "render everything" plus the manifest-presence
  check — correct, since a full rebuild must repopulate every blob.
- The event gate (storage.py:182-200): render `event/{key}` iff the Event row,
  its matches, or its team_events fail `nan_safe_eq` against `orig_objs`, or
  the logical key is absent from the previous manifest.
- The team loop (storage.py:208-222): fetches **all** team_years from the DB,
  buckets non-current-year rows per team, overlays the in-memory current-year
  rows from `objs[1]`, and renders `team/{num}` for every team with a
  current-year team_year. No gate of any kind.
- `_read_team` (`backend/src/site/team.py:37-60`) renders exactly:
  `team.to_dict()` (`src/db/models/team.py:56-79` — name/location/record/
  norm_epa aggregates) plus, per team_year, a fixed 12-field subset
  (norm_epa, unitless_epa, and the rank/percentile fields). So a team blob is
  a pure function of (team row, that team's team_years).
- numpy leak (verified): `post_record_team`
  (`backend/src/models/epa/main.py:191-226`) assigns `np.round` outputs
  directly to `te.epa`, `te.auto_epa`, … `te.tiebreaker_epa`, the
  `comp_{i}_epa` setattr loop, and the mirrored `ty.*` block (lines 201-212
  and 215-226; ~18 assignments per block, loops included). `round(np.float64,
  4)` stays `np.float64`. stdlib json tolerates it (float subclass);
  `orjson.dumps` raises `TypeError` unless `OPT_SERIALIZE_NUMPY`. The
  `match.pre_epas`/`epas` dicts are already `float()`-wrapped, and the enums
  are `(str, Enum)` — both serialize identically under orjson.
- **Additional finding (not in the audit):** `team_to_events`
  (storage.py:202-206) is a dict keyed by **int** team numbers. stdlib json
  silently coerces int keys to strings; `orjson.dumps` raises `TypeError`
  unless `OPT_NON_STR_KEYS` is passed. orjson is therefore *not* a bare
  drop-in — the swap needs `option=orjson.OPT_NON_STR_KEYS`.
- `orjson` is not currently in `backend/pyproject.toml` (python `>=3.9,<3.12`;
  prod 3.11 — binary wheels available).

## 3. Decisions

| Decision | Choice |
|---|---|
| Wire format | **Unchanged**: JSON + zlib (default level), no Content-Encoding. Required by pako/smoke readers; user decision. zstd is out of scope (snapshot-only, see the [snapshot spec](2026-07-20-snapshot-pickle-zstd-design.md)) |
| Team-blob gate | Gate on (current-year TeamYear changed) OR (team row changed vs cycle-start copy) OR (blob absent from previous manifest); render everything when `orig_objs is None` (§4) |
| orjson | **Yes** — swap into `compress()` with `OPT_NON_STR_KEYS` (§5) |
| numpy | **Launder at the source**: `float()`-wrap the ~19 assignment lines in `post_record_team`; do **not** rely on `OPT_SERIALIZE_NUMPY` (§6) |
| Correctness rule | A blob must never be stale — when in doubt, render |

## 4. Team-blob change gate

A team blob depends on three inputs: the team row, the team's current-year
team_year, and its historical team_years. Within a season, on partial cycles:

- **Current-year team_year** changes whenever the team plays (EPA, ranks,
  percentiles). Detectable exactly as the event gate does: `nan_safe_eq`
  (`src/data/utils.py:45-48`) of `objs[1]` vs `orig_objs[1]` per pk. Comparing
  the full row is conservative-correct — it may over-render (a changed field
  the renderer doesn't emit) but can never miss a rendered field.
- **Team row**: not mutated inside `process_year` before `write_objs` runs
  today, but it *can* differ between cycles (post_process/`refresh_teams`
  write the DB) and the [DB retirement spec's](2026-07-20-db-retirement-completion-design.md)
  Phase 1 moves teams publishes after `post_process`, after which in-cycle
  team-row changes become normal. Thread an `orig_teams` copy (deepcopy at
  `src/data/main.py:62`, alongside `orig_objs`, partial cycles only) into
  `write_objs` and compare `nan_safe_eq(team, orig_teams.get(num))`. Cheap
  (one attrs compare per team) and future-proof against the Phase 1 move.
- **Historical team_years**: do not change during partial cycles (only
  `reprocess-year` jobs or full rebuilds touch them, and those paths never run
  `write_objs` for the current year mid-cycle). See the operational rule below.

Predicate (mirrors the event gate's shape):

```python
render_team(num) = (
    orig_objs is None                                  # full rebuild
    or f"team/{num}" not in prev_blobs                 # never published
    or not nan_safe_eq(curr_ty[num], orig_ty.get(num)) # current-year TY changed
    or not nan_safe_eq(teams_by_num[num], orig_teams.get(num))  # team row changed
)
```

where `curr_ty`/`orig_ty` are `{team: TeamYear}` maps built once from
`objs[1]` / `orig_objs[1]`.

Edge cases:

- **Removed team_year** (team present in `orig_objs[1]` but absent from
  `objs[1]`, e.g. TBA registration removal): the render loop iterates current
  team_years, so the blob would silently keep the dropped year row. Compute
  the removed set (`orig_ty.keys() - curr_ty.keys()`) and re-render those
  blobs too (their team row and historical team_years are already in hand).
- **`get_team_years_db` failure**: the loop is already wrapped in
  `_best_effort` (storage.py:209) and skips team blobs entirely on failure —
  the gate changes nothing there.
- **Historical reprocess staleness (behavior change):** today, `make
  reprocess-year YEAR=<past>` is self-healing — the next hourly cycle
  re-renders every team blob and the content-hash diff picks up changed
  history. With the gate, historical changes no longer propagate on partial
  cycles. **Resolution (user decision 2026-07-21): deterministic code, not a
  runbook rule** — the historical-reprocess path itself triggers a full
  current-year render (`reset_curr_year`) when it completes, so no operator
  or agent has to remember anything. Chain it in the reprocess driver/Make
  target as code.

Expected quiet-cycle render count: 11 singletons + 0 events + 0 teams ≈ 11
`compress()` calls, down from ~3,735.

## 5. orjson verdict: yes

With gating in place, quiet cycles compress few blobs — but orjson still pays:

- **The ungated singletons are the big payloads.** `team_years/{CURR_YEAR}`
  serializes ~3,724 full TeamYear dicts and `events/all` the entire event
  history, every cycle, forever — legitimately, since ranks/percentiles shift
  with every match. These dominate remaining quiet-cycle serialization.
- **Busy cycles are the sizing case.** On event days, hundreds of team blobs
  and dozens of event blobs legitimately change per cycle; gating doesn't help
  there. The audit puts json.dumps as the larger share vs zlib for these
  payloads, with orjson at 5–10× on the JSON step
  ([PERF-REPROCESS.md](../rig/PERF-REPROCESS.md) §2.3).
- **Full rebuilds render everything** (~3,735+ blobs per year, plus
  `upload_historical` blobs which share `compress()`, storage.py:312-319) —
  orjson applies each time.
- **Cost is one pure-swap dependency** with prebuilt wheels for prod 3.11.
  Output is byte-different but format-identical JSON — reader-invisible
  (pako `JSON.parse` and smoke `json.loads` both accept it unchanged).

Estimated effect (estimates, not measurements): busy-cycle and full-rebuild
storage serialization drops to roughly the zlib floor — order 2–5× on the JSON
share, i.e. a few seconds per busy cycle and per rebuilt year.

Call shape: `orjson.dumps(data, option=orjson.OPT_NON_STR_KEYS)` (returns
`bytes`; no `.encode()`). `OPT_NON_STR_KEYS` is **required** for the int-keyed
`team_to_events` dict (§2) and coerces int keys to strings exactly as stdlib
does. Do **not** pass `OPT_SERIALIZE_NUMPY` — see §6. One semantic delta:
stdlib emits literal `NaN` tokens for NaN floats (which `JSON.parse` would
reject — so live blobs evidently contain none), while orjson emits `null`;
the parity check in §9 confirms no NaN reaches serialization.

The same swap trivially applies to the per-match-cell `json.dumps` in
`src/db_duckdb/schema.py:63` if the helper is shared, but the parquet path is
out of scope here. The snapshot path moves off JSON entirely
([snapshot spec](2026-07-20-snapshot-pickle-zstd-design.md)).

## 6. numpy laundering

Chosen remedy: `float()`-wrap the assignments in `post_record_team`
(`src/models/epa/main.py:201-212` for `te.*`, `215-226` for `ty.*` — verify
the exact lines at implementation time). Rationale over `OPT_SERIALIZE_NUMPY`:

- It fixes the type at the source, unblocking **every** downstream serializer
  (orjson here, pickle in the snapshot spec, parquet) instead of patching one.
- ~19 trivial edits mirroring the already-correct `result[...] = float(...)`
  pattern in the same function (lines 198, 228-237).
- `OPT_SERIALIZE_NUMPY` would also mask any *future* numpy leak instead of
  surfacing it as a loud `TypeError` in dev.

`nan_safe_eq`'s `_canonical` treats `np.float64` as `float` (it subclasses
float), so laundering does not perturb the change gates.

## 7. Wire format: explicitly unchanged

Blobs remain zlib-compressed JSON bytes uploaded as
`application/octet-stream` with no `Content-Encoding` (storage.py:62-68).
The frontend's `pako.inflate` and the smoke suite's `zlib.decompress` continue
to work with zero coordination. This is a user decision (2026-07-20); any
compression-format change (zstd, gzip Content-Encoding) is a separate,
reader-coordinated project.

## 8. Implementation order (independently shippable PRs)

1. **PR 1 — float laundering + orjson swap.** Launder `post_record_team`;
   add `orjson` to `backend/pyproject.toml`; swap `compress()` (storage.py:48)
   to `orjson.dumps(..., option=orjson.OPT_NON_STR_KEYS)`. Verify per §9.
2. **PR 2 — team-blob change gate.** Thread `orig_teams` from
   `src/data/main.py`; build the `curr_ty`/`orig_ty` maps; apply the §4
   predicate + removed-team handling; add the reprocess-year operational rule
   (Makefile chain or docs). Verify per §9.

Either PR can land first; PR 1 is smaller and de-risks the parity harness.

## 9. Verification

- **Blob parity on an unchanged dataset:** run a cycle before and after each
  PR against the same data; for every logical blob, `zlib.decompress` +
  `json.loads` both versions and compare **parsed-JSON equality** — byte
  equality is expected to fail after PR 1 (orjson's compact separators), and
  parsed equality also flushes out any NaN→null delta (stdlib `json.loads`
  would yield `nan`, orjson-encoded blobs `None`).
- **Gate correctness (PR 2):** on a quiet partial cycle, `plan_uploads`
  should produce zero `team/*` and `event/*` uploads and an unchanged
  manifest hash set; on a cycle with an injected TeamYear change, exactly
  that team's blob re-renders with correct content.
- **Smoke suite passes:** `make smoke` after each deploy (DEPLOY.md §2
  checklist as always).
- **One-time full re-upload (expected):** PR 1 changes every content hash
  once (byte-different JSON), so the first post-deploy cycle re-uploads the
  full blob set and rewrites the manifest. Harmless (versioned keys +
  manifest-last publish, storage.py:107-121); GC reclaims the old versions
  after the 48 h grace (storage.py:274-309). Call this out in the deploy
  notes so it isn't mistaken for a gating bug.
- **Browser check:** load a team page and an event page on the mirror.

## 10. Open and resolved questions

- ~~Chain or document?~~ **Decided 2026-07-21: chain in code** (§4). Prefer
  deterministic code over runbook procedures generally.
- When the [DB retirement](2026-07-20-db-retirement-completion-design.md)
  Phase 1 moves teams publishes after `post_process`, does the team-blob
  render move with it? If so, the `orig_teams` baseline must be captured
  before `post_process` mutates the list — coordinate the two PRs.
- `teams/all` stays ungated (it is one small blob); worth gating only if
  profiling says otherwise.
- The full-history team_years fetch feeding team blobs (storage.py:209) is a
  DB read today and becomes a Parquet/DuckDB read after
  [DB retirement](2026-07-20-db-retirement-completion-design.md) — the "big
  DB read" framing is transitional. Making it lazy (skip when the gate
  renders zero team blobs) is a cheap add in PR 2 and stays worthwhile
  post-retirement; implement if trivial, don't contort for it.
