# Offseason Event Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore offseason event support (TBA type-99, year ≥ 2025, frozen EPA, week 9) on the fork, add a cheap read-triggered freshness ping, deploy to the staging mirror, and verify 2026iri live + 2025cc parity.

**Architecture:** Re-add the pre-removal (`169330e^` / commit `7a3f7ec`) offseason logic to master-compatible files as a clean PR branch, plus a new in-memory-cooldown ping endpoint that wraps the existing `/v3/site/update_curr_year` probe. Merge to `staging` (the deploy branch) with one staging-only commit for the DuckDB read layer, rebuild data, verify in production.

**Tech Stack:** Python/FastAPI backend, Next.js frontend, pytest, GCS bucket-first serving + DuckDB on staging, Cloud Run + Cloud Scheduler.

**Spec:** [2026-07-16-offseason-events-design.md](../specs/2026-07-16-offseason-events-design.md)

## Global Constraints

- Git remotes: `origin` = avgupta456 (pull only, NEVER push), `fork` = chondl (all pushes). PR creation MUST use `gh pr create --repo chondl/statbotics` (gh defaults to upstream otherwise).
- Never open PRs/issues/comments on avgupta456/statbotics; never post to Chief Delphi.
- `docs/` is locally git-excluded — never commit spec/plan files to any branch.
- No API-key/auth code anywhere (user decision; upstream's `apikey.tsx` stays untouched).
- EPA must NEVER be updated by offseason matches (`EventType.OFFSEASON`); `2026isrtp` stays `EventType.DISTRICT` via `EVENT_TYPE_OVERRIDES` and DOES update EPA.
- Ping hot path must be pure in-process memory: no DB, no GCS, no file I/O (user requirement).
- Deploy branch is `staging`; deploy scripts at `docs/superpowers/rig/deploy/deploy.sh` (see `DEPLOY.md` beside it).
- Baseline commit for the PR branch: `master` (`a2cea55`).

---

### Task 1: Worktree, branch, and test scaffolding

**Files:**
- Create: `.worktrees/offseason-events/` (worktree for branch `offseason-events` off master)
- Create: `backend/pytest.ini` (in the worktree)

**Interfaces:**
- Produces: branch `offseason-events`; all later backend tasks run `poetry run pytest tests/ -v` from `.worktrees/offseason-events/backend/`.

- [ ] **Step 1: Create the worktree and branch** (per superpowers:using-git-worktrees)

```bash
cd /Users/chondl/learn/statbotics
git worktree add .worktrees/offseason-events -b offseason-events master
```

- [ ] **Step 2: Add pytest scaffolding** (same convention as the fork's `epa-consistency-tests` branch)

Create `.worktrees/offseason-events/backend/pytest.ini`:

```ini
[pytest]
testpaths = tests
pythonpath = .
```

```bash
mkdir -p .worktrees/offseason-events/backend/tests
```

- [ ] **Step 3: Install backend deps in the worktree**

```bash
cd .worktrees/offseason-events/backend && poetry install
poetry run python -c "import pytest" || poetry run pip install pytest
```

Expected: installs complete; pytest importable.

- [ ] **Step 4: Commit**

```bash
git add pytest.ini
git commit -m "test: add pytest scaffolding"
```

---

### Task 2: Restore offseason ingestion in `read_tba.py`

**Files:**
- Modify: `backend/src/tba/read_tba.py:96-135` (the `get_events` type filter / type map / week logic)
- Modify: `backend/src/api/query.py:25-28` (restore `offseason` in the docstring)
- Test: `backend/tests/test_offseason_ingest.py`

**Interfaces:**
- Consumes: `get_tba(path, etag, cache) -> (data, etag)` from `src.tba.main` (module-level import in read_tba.py — monkeypatchable at `src.tba.read_tba.get_tba`).
- Produces: `get_events(year, etag, cache)` now returns type-99 events for year ≥ 2025 as `EventDict` with `type=EventType.OFFSEASON, week=9`. Later tasks key off `event.type == EventType.OFFSEASON` (template.py, wins.py, noteworthy) and `week == 9` (frontend label).

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_offseason_ingest.py`:

```python
from datetime import datetime, timedelta

import src.tba.read_tba as rt
from src.types.enums import EventType

YEAR = 2026
FUTURE = (datetime.now() + timedelta(days=30)).strftime("%Y-%m-%d")
PAST = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")


def mk_event(key, event_type, week=None, start_date=PAST, end_date=PAST):
    return {
        "key": key,
        "event_type": event_type,
        "district": None,
        "week": week,
        "name": f"Event {key}",
        "country": "USA",
        "state_prov": "IN",
        "start_date": start_date,
        "end_date": end_date,
        "webcasts": [],
    }


def mk_teams(nums):
    return [{"key": f"frc{n}"} for n in nums]


def mk_match(red, blue):
    return {
        "alliances": {
            "red": {"team_keys": [f"frc{t}" for t in red]},
            "blue": {"team_keys": [f"frc{t}" for t in blue]},
        }
    }


def patch_tba(monkeypatch, events, extra=None):
    payloads = {f"events/{YEAR}": events, "events/2024": events, **(extra or {})}

    def _get(path, etag=None, cache=True):
        return payloads.get(path, []), None

    monkeypatch.setattr(rt, "get_tba", _get)


def get_keys(year=YEAR):
    out, _ = rt.get_events(year)
    return {e["key"]: e for e in out}


def test_offseason_event_ingested_as_week9_offseason(monkeypatch):
    patch_tba(
        monkeypatch,
        [mk_event("2026iri", 99)],
        {
            "event/2026iri/teams/simple": mk_teams(range(1, 40)),
            "event/2026iri/matches": [mk_match([1, 2, 3], [4, 5, 6])],
        },
    )
    events = get_keys()
    assert "2026iri" in events
    assert events["2026iri"]["type"] == EventType.OFFSEASON
    assert events["2026iri"]["week"] == 9


def test_preseason_dropped(monkeypatch):
    patch_tba(
        monkeypatch,
        [mk_event("2026week0", 100)],
        {
            "event/2026week0/teams/simple": mk_teams(range(1, 40)),
            "event/2026week0/matches": [mk_match([1, 2, 3], [4, 5, 6])],
        },
    )
    assert "2026week0" not in get_keys()


def test_offseason_before_2025_dropped(monkeypatch):
    patch_tba(
        monkeypatch,
        [mk_event("2024cc", 99)],
        {
            "event/2024cc/teams/simple": mk_teams(range(1, 40)),
            "event/2024cc/matches": [mk_match([1, 2, 3], [4, 5, 6])],
        },
    )
    assert "2024cc" not in get_keys(2024)


def test_under_6_teams_dropped(monkeypatch):
    patch_tba(
        monkeypatch,
        [mk_event("2026tiny", 99)],
        {
            "event/2026tiny/teams/simple": mk_teams([1, 2, 3, 4, 5]),
            "event/2026tiny/matches": [],
        },
    )
    assert "2026tiny" not in get_keys()


def test_placeholder_team_event_dropped(monkeypatch):
    patch_tba(
        monkeypatch,
        [mk_event("2026ph", 99)],
        {
            "event/2026ph/teams/simple": mk_teams([1, 2, 3, 4, 5, 9971]),
            "event/2026ph/matches": [mk_match([1, 2, 3], [4, 5, 9971])],
        },
    )
    assert "2026ph" not in get_keys()


def test_b_team_event_dropped(monkeypatch):
    patch_tba(
        monkeypatch,
        [mk_event("2026bt", 99)],
        {
            "event/2026bt/teams/simple": mk_teams(range(1, 40)),
            "event/2026bt/matches": [
                {
                    "alliances": {
                        "red": {"team_keys": ["frc254B", "frc2", "frc3"]},
                        "blue": {"team_keys": ["frc4", "frc5", "frc6"]},
                    }
                }
            ],
        },
    )
    assert "2026bt" not in get_keys()


def test_matchless_past_event_dropped(monkeypatch):
    patch_tba(
        monkeypatch,
        [mk_event("2026dead", 99, end_date=PAST)],
        {
            "event/2026dead/teams/simple": mk_teams(range(1, 40)),
            "event/2026dead/matches": [],
        },
    )
    assert "2026dead" not in get_keys()


def test_matchless_upcoming_event_kept(monkeypatch):
    patch_tba(
        monkeypatch,
        [mk_event("2026cc", 99, start_date=FUTURE, end_date=FUTURE)],
        {
            "event/2026cc/teams/simple": mk_teams(range(1, 40)),
            "event/2026cc/matches": [],
        },
    )
    events = get_keys()
    assert "2026cc" in events
    assert events["2026cc"]["week"] == 9


def test_event_type_override_beats_offseason(monkeypatch):
    # 2026isrtp is a real entry in EVENT_TYPE_OVERRIDES (-> DISTRICT).
    # It must bypass the offseason path entirely: DISTRICT type, TBA week
    # (+1 TBA bug adjustment), NOT week 9, and it must not require the
    # offseason quality-filter TBA calls.
    patch_tba(monkeypatch, [mk_event("2026isrtp", 99, week=5)])
    events = get_keys()
    assert "2026isrtp" in events
    assert events["2026isrtp"]["type"] == EventType.DISTRICT
    assert events["2026isrtp"]["week"] == 6


def test_regular_regional_unaffected(monkeypatch):
    patch_tba(monkeypatch, [mk_event("2026gal", 0, week=0)])
    events = get_keys()
    assert events["2026gal"]["type"] == EventType.REGIONAL
    assert events["2026gal"]["week"] == 1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd .worktrees/offseason-events/backend && poetry run pytest tests/test_offseason_ingest.py -v`
Expected: FAIL — offseason events are currently dropped (`assert "2026iri" in events` fails, etc.). `test_event_type_override_beats_offseason` and `test_regular_regional_unaffected` may already pass.

- [ ] **Step 3: Implement in `read_tba.py`**

In `backend/src/tba/read_tba.py`, add `PLACEHOLDER_TEAMS` to the constants import (lines 8–13):

```python
from src.tba.constants import (
    DISTRICT_OVERRIDES,
    EVENT_BLACKLIST,
    EVENT_TYPE_OVERRIDES,
    MATCH_BLACKLIST,
    PLACEHOLDER_TEAMS,
)
```

Replace the current lines 96–98:

```python
        event_type_int = int(event["event_type"])
        if event_type_int in (99, 100) and key not in EVENT_TYPE_OVERRIDES:
            continue
```

with (restores `169330e^` behavior, except type-100 preseason stays dropped):

```python
        event_type_int = int(event["event_type"])
        if event_type_int in (99, 100) and key not in EVENT_TYPE_OVERRIDES:
            if event_type_int == 100:
                continue  # preseason
            # offseason events are ingested for 2025+ with quality filters
            if year < 2025:
                continue
            try:
                event_teams = get_event_teams(key, etag=None, cache=cache)[0]
                # remove events with less than 6 teams
                if len(event_teams) < 6:
                    continue
                if len(set(PLACEHOLDER_TEAMS).intersection(set(event_teams))) > 0:
                    continue
                matches = get_tba(f"event/{key}/matches", etag=None, cache=cache)[0]
                end_date = datetime.strptime(event["end_date"], "%Y-%m-%d")
                if len(matches) == 0 and (datetime.now() - end_date).days >= 1:  # type: ignore
                    continue
                for match in matches:  # type: ignore
                    all_teams = match["alliances"]["red"]["team_keys"]
                    all_teams += match["alliances"]["blue"]["team_keys"]
                    all_teams = [int(x[3:]) for x in all_teams]  # asserts no B teams
            except Exception:
                # remove events with B teams
                continue
```

In the `event_type_dict` block (after line 109 `event_type_dict[6] = EventType.EINSTEIN`), add:

```python
        event_type_dict[99] = EventType.OFFSEASON
```

After the champs week-8 block (lines 121–123), add the offseason week assignment. Key off the **final** `event_type` (not the raw int) so `EVENT_TYPE_OVERRIDES` events keep their TBA week:

```python
        # assigns worlds to week 8
        if event_type.is_champs():
            event["week"] = 8

        if event_type == EventType.OFFSEASON:
            event["week"] = 9
```

(`from datetime import datetime` already exists at line 3. The `week += 1` TBA-bug block only touches REGIONAL/DISTRICT/DISTRICT_CMP, so week stays 9.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/test_offseason_ingest.py -v`
Expected: all 11 PASS.

- [ ] **Step 5: Restore the API docstring**

In `backend/src/api/query.py` lines 25–28, change the description back to:

```python
event_type_query = Query(
    None,
    description="One of [`regional`, `district`, `district_cmp`, `champs_div`, `einstein`, or `offseason`].",
)
```

- [ ] **Step 6: Lint and commit**

```bash
poetry run black src/ tests/ && poetry run isort src/ tests/ && poetry run flake8 src/ tests/
git add src/tba/read_tba.py src/api/query.py tests/test_offseason_ingest.py
git commit -m "feat: ingest offseason events (2025+) as week 9 with quality filters"
```

---

### Task 3: EPA freeze for offseason matches in `template.py`

**Files:**
- Modify: `backend/src/models/template.py:77-90`
- Test: `backend/tests/test_offseason_epa_freeze.py`

**Interfaces:**
- Consumes: `EventType.OFFSEASON` from `src.types.enums`; `Event.type` set by Task 2.
- Produces: `Model.process_match` never calls `update_team` for matches at `EventType.OFFSEASON` events; predictions (`record_match`, `match.epas`) still produced.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_offseason_epa_freeze.py`:

```python
from src.db.models import Event, Match, TeamEvent, TeamYear, Year
from src.models.template import Model
from src.models.types import AlliancePred, Attribution
from src.types.enums import CompLevel, EventType, MatchStatus


class RecordingModel(Model):
    def __init__(self):
        super().__init__()
        self.updated = []

    def predict_match(self, match, event):
        return 0.5, AlliancePred(10.0, None), AlliancePred(10.0, None)

    def attribute_match(self, match, red_pred, blue_pred):
        return {t: Attribution() for t in match.get_red() + match.get_blue()}

    def update_team(self, team, attrib, match):
        self.updated.append(team)


def mk_match(event_key, week):
    return Match(
        key=f"{event_key}_qm1",
        year=2026,
        event=event_key,
        week=week,
        elim=False,
        comp_level=CompLevel.QUAL,
        set_number=1,
        match_number=1,
        time=0,
        status=MatchStatus.COMPLETED,
        red_1=1,
        red_2=2,
        red_3=3,
        blue_1=4,
        blue_2=5,
        blue_3=6,
        red_dq="",
        red_surrogate="",
        blue_dq="",
        blue_surrogate="",
        red_score=20,
        blue_score=10,
        red_no_foul=20,
        blue_no_foul=10,
    )


def run_model(event_type, week):
    model = RecordingModel()
    model.start_season(Year(year=2026), {}, {})
    event = Event(key="2026x", year=2026, name="X", type=event_type, week=week)
    match = mk_match("2026x", week)
    teams = [1, 2, 3, 4, 5, 6]
    team_events = {t: TeamEvent(team=t, year=2026, event="2026x") for t in teams}
    team_years = {t: TeamYear(team=t, year=2026) for t in teams}
    model.process_match(match, event, team_events, team_years)
    return model, match


def test_offseason_match_skips_epa_update():
    model, match = run_model(EventType.OFFSEASON, 9)
    assert model.updated == []
    # predictions and post-match records are still produced
    assert match.pre_epas is not None
    assert match.epas is not None


def test_regular_match_updates_epa():
    model, _ = run_model(EventType.REGIONAL, 1)
    assert sorted(model.updated) == [1, 2, 3, 4, 5, 6]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/test_offseason_epa_freeze.py -v`
Expected: `test_offseason_match_skips_epa_update` FAILS (updated == all 6 teams); the regular-match test passes.

- [ ] **Step 3: Implement**

In `backend/src/models/template.py`, add `EventType` to the enums import (line 6):

```python
from src.types.enums import EventType, MatchStatus
```

Replace lines 77–90:

```python
        # Don't update if 1) placeholder match, 2) elim dq, 3) all fouls
        teams = set(match.get_red() + match.get_blue())
```

with:

```python
        # Don't update if 1) offseason, 2) placeholder match, 3) elim dq, 4) all fouls
        offseason_event = event.type == EventType.OFFSEASON
        teams = set(match.get_red() + match.get_blue())
```

and change line 90:

```python
        skip_update = placeholder_match or elim_dq or all_fouls
```

to:

```python
        skip_update = offseason_event or placeholder_match or elim_dq or all_fouls
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/test_offseason_epa_freeze.py -v`
Expected: 2 PASS.

- [ ] **Step 5: Commit**

```bash
poetry run black src/ tests/ && poetry run isort src/ tests/ && poetry run flake8 src/ tests/
git add src/models/template.py tests/test_offseason_epa_freeze.py
git commit -m "feat: freeze EPA at offseason events (predict, never update)"
```

---

### Task 4: Exclude offseason matches from W-L records in `wins.py`

**Files:**
- Modify: `backend/src/data/wins.py:20-35` (match loop guard)
- Test: `backend/tests/test_offseason_records.py`

**Interfaces:**
- Consumes: `objs_type` tuple `(Year, Dict[str, TeamYear], Dict[str, Event], Dict[str, TeamEvent], Dict[str, Match], Dict[str, ETag])` from `src.data.utils`.
- Produces: `process_year(objs)` leaves TeamYear/TeamEvent W-L records at 0 for offseason matches. Note: the date-based `next_event` logic (lines ~143-151) intentionally KEEPS offseason events — a team's next event may be IRI, which is desired on the mirror.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_offseason_records.py`:

```python
from src.data.wins import process_year
from src.db.models import Event, Match, TeamEvent, TeamYear, Year
from src.types.enums import CompLevel, EventType, MatchStatus, MatchWinner


def mk_objs(event_type, week):
    year = Year(year=2026)
    event = Event(
        key="2026x",
        year=2026,
        name="X",
        type=event_type,
        week=week,
        start_date="2026-07-16",
        end_date="2026-07-18",
    )
    match = Match(
        key="2026x_qm1",
        year=2026,
        event="2026x",
        week=week,
        elim=False,
        comp_level=CompLevel.QUAL,
        set_number=1,
        match_number=1,
        time=0,
        status=MatchStatus.COMPLETED,
        red_1=1,
        red_2=2,
        red_3=3,
        blue_1=4,
        blue_2=5,
        blue_3=6,
        red_dq="",
        red_surrogate="",
        blue_dq="",
        blue_surrogate="",
        winner=MatchWinner.RED,
        red_score=20,
        blue_score=10,
    )
    ty = TeamYear(team=1, year=2026)
    te = TeamEvent(team=1, year=2026, event="2026x")
    return (
        year,
        {"2026_1": ty},
        {"2026x": event},
        {"1_2026x": te},
        {"2026x_qm1": match},
        {},
    )


def test_offseason_match_excluded_from_records():
    objs = mk_objs(EventType.OFFSEASON, 9)
    process_year(objs)
    ty = objs[1]["2026_1"]
    te = objs[3]["1_2026x"]
    assert (ty.wins, ty.losses, ty.ties, ty.count) == (0, 0, 0, 0)
    assert te.count == 0


def test_regular_match_counted():
    objs = mk_objs(EventType.REGIONAL, 1)
    process_year(objs)
    ty = objs[1]["2026_1"]
    assert (ty.wins, ty.count) == (1, 1)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `poetry run pytest tests/test_offseason_records.py -v`
Expected: `test_offseason_match_excluded_from_records` FAILS (wins == 1); regular test passes.

- [ ] **Step 3: Implement**

In `backend/src/data/wins.py`, add `EventType` to the enums import (line 8):

```python
from src.types.enums import EventType, MatchStatus, MatchWinner
```

After line 21 (`year_num = objs[0].year`), add:

```python
    event_to_type = {e.key: e.type for e in objs[2].values()}
```

Replace lines 34–35:

```python
        if status != MatchStatus.COMPLETED or winner is None:
            continue
```

with:

```python
        if (
            event_to_type[event] == EventType.OFFSEASON
            or status != MatchStatus.COMPLETED
            or winner is None
        ):
            continue
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/test_offseason_records.py -v`
Expected: 2 PASS. Also run the full suite: `poetry run pytest tests/ -v` — all green.

- [ ] **Step 5: Commit**

```bash
poetry run black src/ tests/ && poetry run isort src/ tests/ && poetry run flake8 src/ tests/
git add src/data/wins.py tests/test_offseason_records.py
git commit -m "feat: exclude offseason matches from season W-L records"
```

---

### Task 5: Exclude offseason from noteworthy matches (SQLAlchemy layer)

**Files:**
- Modify: `backend/src/db/functions/noteworthy_matches.py:8-32`

**Interfaces:**
- Produces: noteworthy-match queries (`high_score`, etc.) never return offseason matches (offseason games have modified rules; their scores would pollute season leaderboards). This restores the `169330e`-removed filter. The staging DuckDB twin is fixed in Task 9.

No unit test (requires a live DB); verified in the Task 12 production pass.

- [ ] **Step 1: Implement**

In `backend/src/db/functions/noteworthy_matches.py`, change the imports (line 10):

```python
from src.types.enums import EventType, MatchStatus
```

and the base filter (lines 28–32):

```python
        ).filter(
            (MatchORM.year == year)
            & (MatchORM.status == MatchStatus.COMPLETED)
            & (MatchORM.event == EventORM.key)
            & (EventORM.type != EventType.OFFSEASON)
        )
```

- [ ] **Step 2: Lint and commit**

```bash
poetry run black src/ && poetry run isort src/ && poetry run flake8 src/
git add src/db/functions/noteworthy_matches.py
git commit -m "feat: exclude offseason matches from noteworthy matches"
```

---

### Task 6: Read-triggered freshness ping endpoint

**Files:**
- Modify: `backend/src/data/router.py`
- Test: `backend/tests/test_ping.py`

**Interfaces:**
- Consumes: existing `update_curr_year_background()` self-HTTP pattern and the existing `GET /v3/site/update_curr_year` probe endpoint (present on both master and staging — staging's snapshot-based probe body is reused untouched, which is what makes this merge-conflict-free).
- Produces: `GET /v3/site/ping/event/{event_key}` → 202 (probe scheduled) or 204 (cooldown/in-flight/invalid key). Frontend (Task 7) calls it fire-and-forget.

**Design (user-approved direction):** hot path is pure in-memory — module globals `_ping_last_probe` / `_ping_inflight` (valid because the data service is structurally single-worker: `gunicorn -w 1`). On a cold ping, a FastAPI background task self-HTTPs the existing `/v3/site/update_curr_year` probe (etag pre-check → backgrounded partial cycle only if TBA changed). Global 300 s cooldown = at most one probe per 5 min regardless of viewer count; every other ping is a float compare + 204.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_ping.py`:

```python
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

import src.data.router as dr
from src.constants import CURR_YEAR


def make_client(monkeypatch, last_probe=float("-inf"), inflight=False):
    calls = []
    monkeypatch.setattr(
        dr.requests, "get", lambda url, **kw: calls.append(url)
    )
    monkeypatch.setattr(dr, "_ping_last_probe", last_probe)
    monkeypatch.setattr(dr, "_ping_inflight", inflight)
    app = FastAPI()
    app.include_router(dr.site_router, prefix="/v3/site")
    return TestClient(app), calls


def test_cold_ping_schedules_probe(monkeypatch):
    client, calls = make_client(monkeypatch)
    resp = client.get(f"/v3/site/ping/event/{CURR_YEAR}iri")
    assert resp.status_code == 202
    # TestClient runs background tasks before returning
    assert len(calls) == 1
    assert calls[0].endswith("/v3/site/update_curr_year")


def test_ping_within_cooldown_is_noop(monkeypatch):
    client, calls = make_client(monkeypatch, last_probe=time.monotonic())
    resp = client.get(f"/v3/site/ping/event/{CURR_YEAR}iri")
    assert resp.status_code == 204
    assert calls == []


def test_ping_while_inflight_is_noop(monkeypatch):
    client, calls = make_client(monkeypatch, inflight=True)
    resp = client.get(f"/v3/site/ping/event/{CURR_YEAR}iri")
    assert resp.status_code == 204
    assert calls == []


def test_second_ping_hits_cooldown(monkeypatch):
    client, calls = make_client(monkeypatch)
    assert client.get(f"/v3/site/ping/event/{CURR_YEAR}iri").status_code == 202
    assert client.get(f"/v3/site/ping/event/{CURR_YEAR}iri").status_code == 204
    assert len(calls) == 1


def test_non_current_year_key_rejected(monkeypatch):
    client, calls = make_client(monkeypatch)
    resp = client.get("/v3/site/ping/event/2019ncwak")
    assert resp.status_code == 204
    assert calls == []


def test_malformed_key_rejected(monkeypatch):
    client, calls = make_client(monkeypatch)
    resp = client.get(f"/v3/site/ping/event/{CURR_YEAR}IRI!")
    assert resp.status_code == 204
    assert calls == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `poetry run pytest tests/test_ping.py -v`
Expected: FAIL — 404 (route does not exist).

- [ ] **Step 3: Implement**

In `backend/src/data/router.py`, extend the imports (lines 1–2):

```python
import re
import time

import requests
from fastapi import APIRouter, BackgroundTasks, Response
```

Append at the end of the file (after the existing `update_curr_year_site_endpoint`):

```python
# Read-triggered freshness ping.
#
# Event pages fire-and-forget GET /v3/site/ping/event/{key} while an event is
# live. The hot path below is pure in-process memory (no DB, no GCS, no TBA):
# during the cooldown or while a probe is in flight, a ping costs a regex, a
# float compare, and a 204. The data service runs a single gunicorn worker by
# design (structurally single-writer), so module globals are authoritative.
#
# A cold ping schedules a background self-HTTP to /v3/site/update_curr_year —
# the existing cheap probe (TBA etag pre-check, then a backgrounded partial
# cycle only if something actually changed). The 300s cooldown bounds TBA
# traffic to one probe per 5 minutes no matter how many viewers pile on.
PING_COOLDOWN_S = 300

_ping_last_probe: float = float("-inf")
_ping_inflight: bool = False


def _ping_probe():
    global _ping_inflight
    try:
        requests.get(f"{BACKEND_URL}/v3/site/update_curr_year")
    finally:
        _ping_inflight = False


@site_router.get("/ping/event/{event_key}")
async def ping_event_endpoint(event_key: str, background_tasks: BackgroundTasks):
    global _ping_last_probe, _ping_inflight
    if not re.fullmatch(rf"{CURR_YEAR}[a-z0-9]+", event_key):
        return Response(status_code=204)
    now = time.monotonic()
    if _ping_inflight or now - _ping_last_probe < PING_COOLDOWN_S:
        return Response(status_code=204)
    _ping_last_probe = now
    _ping_inflight = True
    background_tasks.add_task(_ping_probe)
    return Response(status_code=202)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `poetry run pytest tests/test_ping.py -v`
Expected: 6 PASS. Full suite: `poetry run pytest tests/ -v` — all green.

- [ ] **Step 5: Commit**

```bash
poetry run black src/ tests/ && poetry run isort src/ tests/ && poetry run flake8 src/ tests/
git add src/data/router.py tests/test_ping.py
git commit -m "feat: read-triggered freshness ping with in-memory cooldown"
```

---

### Task 7: Frontend — offseason labels and event-page ping

**Files:**
- Modify: `frontend/src/components/filterConstants.tsx:125-136` (weekOptions)
- Modify: `frontend/src/pagesContent/events/summary.tsx:32` (week label)
- Modify: `frontend/src/pages/event/[event_id].tsx` (ping effect)

**Interfaces:**
- Consumes: `BACKEND_URL`, `CURR_YEAR` from `frontend/src/constants.tsx`; ping endpoint from Task 6; `data.event.start_date` / `end_date` / `year` from the event payload.
- Produces: "Offseason" appears in the shared week FilterBar and on event cards; live event pages ping the backend.

- [ ] **Step 1: weekOptions** — in `frontend/src/components/filterConstants.tsx`, add after the `{ value: 8, label: "Week 8" },` line:

```tsx
  { value: 9, label: "Offseason" },
```

- [ ] **Step 2: Event-card label** — in `frontend/src/pagesContent/events/summary.tsx` line 32, change:

```tsx
  const weekStr = `Week ${event.week}`;
```

to:

```tsx
  const weekStr = event.week === 9 ? "Offseason" : `Week ${event.week}`;
```

- [ ] **Step 3: Event-page ping** — in `frontend/src/pages/event/[event_id].tsx`, extend the constants import (add to existing imports near line 10):

```tsx
import { BACKEND_URL, CURR_YEAR } from "../../constants";
```

Add a new effect after the `document.title` useEffect (line ~42):

```tsx
  useEffect(() => {
    // Fire-and-forget freshness ping for live current-year events. The
    // backend absorbs bursts with an in-memory cooldown, so this is safe to
    // call on every page view.
    if (!event_id || !data?.event || data.event.year !== CURR_YEAR) return;
    const today = new Date();
    const start = new Date(`${data.event.start_date}T00:00:00`);
    const end = new Date(`${data.event.end_date}T23:59:59`);
    start.setDate(start.getDate() - 1);
    end.setDate(end.getDate() + 1);
    if (today < start || today > end) return;
    fetch(`${BACKEND_URL}/v3/site/ping/event/${event_id}`).catch(() => {});
  }, [event_id, data]);
```

- [ ] **Step 4: Verify the build**

```bash
cd .worktrees/offseason-events/frontend && yarn install && yarn build
```

Expected: `next build` completes with no type errors.

- [ ] **Step 5: Commit**

```bash
git add src/components/filterConstants.tsx src/pagesContent/events/summary.tsx "src/pages/event/[event_id].tsx"
git commit -m "feat: offseason week label/filter and live-event freshness ping"
```

---

### Task 8: Draft PR on the fork

- [ ] **Step 1: Push and open the draft PR**

```bash
git push -u fork offseason-events
gh pr create --repo chondl/statbotics --draft --base master --head offseason-events \
  --title "[offseason] Restore offseason event support (frozen EPA) + freshness ping" \
  --body "Restores the pre-#412 offseason behavior (TBA type-99, year >= 2025, week 9, EventType.OFFSEASON) with the PR #400/7a3f7ec quality filters; EPA is never updated by offseason matches; offseason excluded from W-L records and noteworthy matches; EVENT_TYPE_OVERRIDES (2026isrtp -> DISTRICT) preserved and EPA-updating. Adds GET /v3/site/ping/event/{key}: in-memory-cooldown wrapper around the existing /v3/site/update_curr_year probe, pinged fire-and-forget by live event pages. Frontend: Offseason week label + filter option. Tests: pytest suite covering ingestion filter matrix, EPA freeze, record exclusion, ping cooldown."
```

Expected: draft PR URL on chondl/statbotics. Do NOT merge yet — merge happens in Task 13 after production verification.

---

### Task 9: Merge to staging + DuckDB noteworthy exclusion

**Files:**
- Modify (staging worktree `.worktrees/staging/`): merge commit + `backend/src/db_duckdb/main.py:444-462`

**Interfaces:**
- Consumes: branch `offseason-events`; staging worktree at `.worktrees/staging` (head `a4a9c8a`).
- Produces: `staging` branch containing all offseason changes; DuckDB `get_noteworthy_matches` excludes offseason (twin of Task 5).

- [ ] **Step 1: Merge**

```bash
cd /Users/chondl/learn/statbotics/.worktrees/staging
git merge offseason-events
```

Expected: clean auto-merge or small conflicts in `backend/src/tba/read_tba.py` (staging added `defer_missing_breakdown` above `get_events`) and `backend/src/data/router.py` (staging's snapshot-based probe endpoint). Resolution rule: keep BOTH sides — staging's probe body and snapshot reads are untouched by our changes; our additions are new code paths. If `read_tba.py` conflicts, our type-99 block replaces the same 3 lines staging also has (identical content, shifted line numbers).

- [ ] **Step 2: Run the combined test suite on staging**

```bash
cd backend && poetry run pytest tests/ -v
```

Expected: our new tests PLUS staging's existing tests (`test_publish.py` etc. if present on staging) all pass. If staging's probe endpoint body diverges enough that `test_ping.py`'s self-HTTP assertion needs its monkeypatch target adjusted, fix the test, not the endpoint.

- [ ] **Step 3: DuckDB noteworthy exclusion (staging-only commit)**

In `.worktrees/staging/backend/src/db_duckdb/main.py`, `get_noteworthy_matches` (line ~452), change:

```python
    where = ["m.year = ?", "m.status = ?"]
    params: List[Any] = [year, MatchStatus.COMPLETED.value]
```

to:

```python
    where = ["m.year = ?", "m.status = ?", "e.type != ?"]
    params: List[Any] = [year, MatchStatus.COMPLETED.value, EventType.OFFSEASON.value]
```

and add `EventType` to the file's `src.types.enums` import.

- [ ] **Step 4: Commit and push staging**

```bash
git add backend/src/db_duckdb/main.py
git commit -m "staging: exclude offseason from DuckDB noteworthy matches"
git push fork staging
```

---

### Task 10: Baselines + deploy to the mirror

**Interfaces:**
- Consumes: deploy scripts `docs/superpowers/rig/deploy/deploy.sh` (steps `backend`, `frontend`); staging API at `https://api-statbotics.iterativerefinement.com`.
- Produces: new code live on the mirror; pre-change 2025 EPA baselines saved for the invariance check.

- [ ] **Step 1: Capture pre-deploy 2025 baselines** (BEFORE deploying)

```bash
SCRATCH=/private/tmp/claude-501/-Users-chondl-learn-statbotics/71deffeb-976b-40d7-8677-49e438599a9a/scratchpad
for t in 254 1678 118 2056 148; do
  curl -s "https://api-statbotics.iterativerefinement.com/v3/team_year/$t/2025" > "$SCRATCH/baseline_ty_${t}_2025.json"
done
curl -s -o /dev/null -w "%{http_code}\n" "https://api-statbotics.iterativerefinement.com/v3/event/2025cc"
```

Expected: baseline files saved; `/v3/event/2025cc` returns 404 or an error (not yet ingested).

- [ ] **Step 2: Deploy backend and frontend**

Read `docs/superpowers/rig/deploy/DEPLOY.md` first, then:

```bash
cd /Users/chondl/learn/statbotics/.worktrees/staging
bash /Users/chondl/learn/statbotics/docs/superpowers/rig/deploy/deploy.sh backend
bash /Users/chondl/learn/statbotics/docs/superpowers/rig/deploy/deploy.sh frontend
```

Expected: Cloud Run revisions deploy green. The hourly Cloud Scheduler (`statbotics-update` → `/v3/site/update_curr_year`) is already configured — do not touch it.

---

### Task 11: Data rebuild (2026 recompute + 2025 backfill)

**Interfaces:**
- Consumes: `docs/superpowers/deliverables/historical-backfill.md` (the authoritative procedure — READ IT FIRST; it covers `DATABASE_URL` via cloud-sql-proxy, `TBA_AUTH_KEY`, `GCS_BUCKET`, quota-project gotcha, resumability via the TBA pickle cache).
- Produces: 2026 offseason events live in current-year blobs/parquet; 2025 offseason events (`2025cc`, `2025iri`, …) in hist blobs + 2025 parquet.

- [ ] **Step 1: Full-history rebuild with the new code** (Step 1 of the backfill doc)

From `.worktrees/staging/backend/` with `DATABASE_URL`/`TBA_AUTH_KEY`/`PYTHONPATH` set per the doc, run as a logged background process:

```python
import src.db.models
import src.data.main as dm
dm.DISABLE_GCS = True
dm.reset_all_years()
```

Expected: rebuild completes (fast on the warm TBA pickle cache from the July 10 load; new TBA fetches only for the offseason event checks). 2025 and 2026 offseason events now have DB rows.

- [ ] **Step 2: Current-year reseed + publish** (Step 2 of the doc)

```python
import src.db.models
from src.data.main import update_curr_year
update_curr_year(partial=False, tba_partial=False)
```

(with `GCS_BUCKET` set, GCS enabled). Expected: manifest advances; `event/2026iri` blob exists in the staging bucket.

- [ ] **Step 3: 2025 hist blob backfill** (Step 3 of the doc)

```bash
python backfill_blobs.py 2025 --force
```

Expected: `event/2025cc`, `event/2025iri` hist blobs written.

- [ ] **Step 4: 2025 parquet re-publish** — the API serves via DuckDB parquet. Verify `curl https://api-statbotics.iterativerefinement.com/v3/event/2025cc` returns data. If it 404s, the 2025 parquet was not republished: read `backend/src/google/parquet.py` (`write_parquet`, lines 65-90 on staging) and `backfill_blobs.py` to determine which publishes historical parquet, run it for 2025, and re-verify the curl.

---

### Task 12: Production verification

All checks against the live mirror. Record outputs; every claim in the final walkthrough must cite one.

- [ ] **Step 1: 2026 IRI live** (user's headline requirement)

```bash
curl -s "https://api-statbotics.iterativerefinement.com/v3/event/2026iri" | python3 -m json.tool | head -30
curl -s "https://api-statbotics.iterativerefinement.com/v3/matches?event=2026iri" | python3 -c "import json,sys; ms=json.load(sys.stdin); print(len(ms), 'matches'); print([m['pred']['red_score'] for m in ms[:3]])"
```

Expected: event JSON with `week: 9`, `type: offseason`; matches with non-null predictions. Then load `https://statbotics.iterativerefinement.com/event/2026iri` in a real browser (superpowers-chrome), confirm schedule + predictions render, screenshot for the walkthrough.

- [ ] **Step 2: 2025 Chezy Champs parity** (user's second requirement)

Write a scratchpad script that: downloads `https://storage.googleapis.com/site_v1/event/2025cc` (upstream prod, zlib-compressed JSON — the known-good pre-removal output) and the staging equivalent (resolve via staging bucket manifest or `hist/{epoch}/event/2025cc`), decompresses both, and compares: match count (expect 84), per-match `pred.red_score`/`blue_score`/`red_win_prob`, and per-team `team_events[].epa`. Expect equality within float tolerance (1e-3). Investigate any mismatch before proceeding — do not rationalize it away.

- [ ] **Step 3: 2025 EPA invariance**

```bash
for t in 254 1678 118 2056 148; do
  curl -s "https://api-statbotics.iterativerefinement.com/v3/team_year/$t/2025" > "$SCRATCH/after_ty_${t}_2025.json"
done
python3 - "$SCRATCH" <<'EOF'
import json, sys
scratch = sys.argv[1]
def flatten(d, prefix=""):
    out = {}
    for k, v in d.items():
        if isinstance(v, dict):
            out.update(flatten(v, f"{prefix}{k}."))
        else:
            out[f"{prefix}{k}"] = v
    return out
for t in (254, 1678, 118, 2056, 148):
    b = flatten(json.load(open(f"{scratch}/baseline_ty_{t}_2025.json")))
    a = flatten(json.load(open(f"{scratch}/after_ty_{t}_2025.json")))
    diffs = {k: (b.get(k), a.get(k)) for k in set(b) | set(a) if b.get(k) != a.get(k)}
    print(t, "IDENTICAL" if not diffs else f"DIFFS: {diffs}")
EOF
```

Expected: `IDENTICAL` for all 5 teams — the entire team-year payload (EPA, norm_epa, W-L record, ranks) must be byte-equal to the pre-deploy baseline, since offseason matches neither update EPA nor count toward records. Any diff is a stop-and-investigate finding, not a tolerance call.

- [ ] **Step 4: API sanity pass — offseason vs regular parity** (user request)

```bash
API=https://api-statbotics.iterativerefinement.com
curl -s -o /dev/null -w "event/2026iri %{http_code}\n"      "$API/v3/event/2026iri"
curl -s -o /dev/null -w "event/2025cc %{http_code}\n"        "$API/v3/event/2025cc"
curl -s -o /dev/null -w "matches?event=2026iri %{http_code}\n" "$API/v3/matches?event=2026iri"
curl -s -o /dev/null -w "team_events?event=2026iri %{http_code}\n" "$API/v3/team_events?event=2026iri"
curl -s "$API/v3/events?year=2026&week=9" | python3 -c "import json,sys; print(len(json.load(sys.stdin)), 'offseason events')"
# regular-season control: pick a champs-division key from /v3/events?year=2026&week=8
curl -s "$API/v3/events?year=2026&week=8" | python3 -c "import json,sys; print([e['key'] for e in json.load(sys.stdin)][:3])"
```

Expected: all 200s, offseason event count ≈ 30-58 (those passing quality filters), identical JSON shape between an offseason and a champs event, `2026isrtp` typed `district`, and site noteworthy-matches endpoint free of offseason matches.

- [ ] **Step 5: Ping behavior + freshness timing**

```bash
curl -s -o /dev/null -w "ping1 %{http_code} %{time_total}s\n" "$API/v3/site/ping/event/2026iri"
curl -s -o /dev/null -w "ping2 %{http_code} %{time_total}s\n" "$API/v3/site/ping/event/2026iri"
curl -s -o /dev/null -w "bad   %{http_code} %{time_total}s\n" "$API/v3/site/ping/event/2019ncwak"
```

Expected: 202 then 204 then 204, each well under 1 s. Then confirm end-to-end freshness: check Cloud Run logs (`gcloud run services logs read` for the api service, project `statbotics-staging`) that ping1 triggered `/v3/site/update_curr_year` → (if TBA changed) `/v3/data/update_curr_year`, and that the bucket `manifest.json` generation advanced within ~5 min. IRI runs today, so real match results should be flowing.

- [ ] **Step 6: Regression sweep** — regular-season pages unaffected: load `/` (teams), `/events?year=2026`, one champs event page, one team page in the browser; no errors, EPAs sane.

---

### Task 13: Walkthrough writeup, monitoring assessment, PR merge

- [ ] **Step 1: Assess logging/monitoring** (user request — assessment ONLY, change nothing)

Inventory what exists for the mirror (project `statbotics-staging`): `gcloud monitoring uptime list-configs`, `gcloud alpha monitoring policies list`, `gcloud monitoring channels list`, Cloud Scheduler job status, Cloud Run default logging/metrics. Determine specifically: if the API went down or the ETL started failing, would anything email chondl@gmail.com? (Expected finding: Cloud Run logs exist but no uptime checks / alerting policies / notification channels — confirm rather than assume.)

- [ ] **Step 2: Write the walkthrough** to `docs/superpowers/status/2026-07-16-offseason-rollout.md` covering: (a) what shipped, file-by-file summary; (b) the ping approach chosen and why (in-memory cooldown wrapping the existing probe; performance envelope: hot-path cost, worst-case TBA traffic 1 probe/5 min, behavior while a cycle blocks the single worker); (c) verification evidence — every Task 12 output; (d) the monitoring inventory + gaps and concrete recommended next steps (uptime check on `/v3/site/health` or `/info`, alert policy → email channel chondl@gmail.com), NOT implemented; (e) anything deferred. Cross-link the spec and this plan as Markdown links.

- [ ] **Step 3: Merge the PR** (user pre-authorized once production-verified)

Only if every Task 12 check passed:

```bash
gh pr ready <PR-number> --repo chondl/statbotics
gh pr merge <PR-number> --repo chondl/statbotics --merge
```

If anything failed, leave the PR in draft and surface the failure in the walkthrough instead.

- [ ] **Step 4: Update memory** — update `project_offseason_events_restore.md` (deployed state, PR number, walkthrough location, any deviations).
