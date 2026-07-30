# Per-Event Offseason EPA Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At an offseason event, each team's EPA forks from its current rating, evolves across that event's matches, and drives that event's predictions, rankings, and simulation — without touching season EPA, next-season seeds, or any other offseason event.

**Architecture:** The EPA model reads exactly two mutable dicts, `self.epas` and `self.counts`. `Model.process_match` wraps each offseason match in a context manager that swaps both for event-scoped copy-on-first-touch mappings, so no EPA math changes. Containment is three edits in `data/epa/agg.py` plus passing `ty=None` to `post_record_team` on offseason matches.

**Tech Stack:** Python 3.11, FastAPI, numpy, pytest, poetry (backend); Next.js 13, TypeScript, Tailwind (frontend).

**Spec:** [2026-07-30 per-event offseason EPA design](../specs/2026-07-30-per-event-offseason-epa-design.md)

## Global Constraints

- `SANDBOX_SEED_COUNT = 0`. `percent_func` spans 0.333→0.200 and clamps at 12 matches, so 12 and a full season count are the same setting.
- Every ingested offseason event gets the fork (2025+). No year branch in the model.
- Offseason events are identified by `event.type == EventType.OFFSEASON`, never by `week == 9`.
- Season EPA, `norm_epa`, ranks, percentiles, and `Team.norm_epa*` must be bit-identical before and after, for every year.
- Run tests with `cd backend && poetry run python -m pytest tests/ -q` from the worktree root.
- Deploy only via `make ship` from `docs/superpowers/rig/deploy/`.

---

### Task 1: Sandbox fork in the model

**Files:**
- Modify: `backend/src/models/epa/constants.py`
- Modify: `backend/src/models/template.py:54-101`
- Modify: `backend/src/models/epa/main.py:22-52`
- Test: `backend/tests/test_offseason_sandbox_epa.py` (renamed from `test_offseason_epa_freeze.py`)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `SANDBOX_SEED_COUNT: int`; `Model.enter_sandbox(event_key: str) -> None`; `Model.exit_sandbox() -> None`; `EPA._sandboxes: Dict[str, Tuple[Dict[int, EPARating], Dict[int, int]]]`. Task 2 relies on `TeamEvent.epa` carrying sandbox values for offseason events.

- [ ] **Step 1: Delete the obsolete freeze test and write the failing replacement**

The old file asserts the exact behavior this task removes, so it is replaced, not amended.

```bash
git rm backend/tests/test_offseason_epa_freeze.py
```

Create `backend/tests/test_offseason_sandbox_epa.py`:

```python
from collections import defaultdict

import numpy as np

from src.db.models import Event, Match, TeamEvent, TeamYear, Year
from src.models.epa.main import EPA
from src.models.epa.math import EPARating
from src.models.template import Model
from src.models.types import AlliancePred, Attribution
from src.types.enums import CompLevel, EventType, MatchStatus


class RecordingModel(Model):
    def __init__(self):
        super().__init__()
        self.updated = []
        self.recorded_tys = []

    def predict_match(self, match, event):
        return 0.5, AlliancePred(10.0, None), AlliancePred(10.0, None)

    def attribute_match(self, match, red_pred, blue_pred):
        return {t: Attribution() for t in match.get_red() + match.get_blue()}

    def update_team(self, team, attrib, match):
        self.updated.append(team)

    def post_record_team(self, team, te, ty):
        self.recorded_tys.append(ty)
        return {}


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


def test_offseason_match_updates_rating():
    """Offseason matches now update -- into sandbox state, which the base
    Model has none of, so update_team still fires."""
    model, match = run_model(EventType.OFFSEASON, 9)
    assert sorted(model.updated) == [1, 2, 3, 4, 5, 6]
    assert match.pre_epas is not None
    assert match.epas is not None


def test_offseason_match_does_not_stamp_team_year():
    model, _ = run_model(EventType.OFFSEASON, 9)
    assert model.recorded_tys == [None] * 6


def test_regular_match_updates_epa_and_stamps_team_year():
    model, _ = run_model(EventType.REGIONAL, 1)
    assert sorted(model.updated) == [1, 2, 3, 4, 5, 6]
    assert all(ty is not None for ty in model.recorded_tys)


def mk_epa_model():
    """EPA with rating state installed directly. start_season needs a fully
    populated Year (get_init_epa reads mean components), which these tests do
    not exercise."""
    model = EPA()
    model.year_num = 2026
    model.num_teams = 3
    model.epas = defaultdict(lambda: EPARating(np.zeros(18)))
    model.counts = defaultdict(int)
    model.epas[254] = EPARating(np.array([100.0] + [0.0] * 17))
    model.counts[254] = 40
    return model


def test_sandbox_forks_on_first_touch_and_leaves_real_state_alone():
    model = mk_epa_model()
    model.enter_sandbox("2026cc")
    model.epas[254].mean[0] = 999.0
    model.counts[254] += 1
    model.exit_sandbox()

    assert model.epas[254].mean[0] == 100.0
    assert model.counts[254] == 40


def test_sandbox_seeds_count_from_constant():
    from src.models.epa.constants import SANDBOX_SEED_COUNT

    model = mk_epa_model()
    model.enter_sandbox("2026cc")
    assert model.counts[254] == SANDBOX_SEED_COUNT
    model.exit_sandbox()


def test_two_offseason_events_are_independent():
    model = mk_epa_model()

    model.enter_sandbox("2026iri")
    model.epas[254].mean[0] = 150.0
    model.exit_sandbox()

    model.enter_sandbox("2026cc")
    assert model.epas[254].mean[0] == 100.0  # seeded from real, not from IRI
    model.exit_sandbox()

    model.enter_sandbox("2026iri")
    assert model.epas[254].mean[0] == 150.0  # IRI kept its own evolution
    model.exit_sandbox()


def test_sandbox_restored_after_exception():
    model = mk_epa_model()
    real_epas, real_counts = model.epas, model.counts

    event = Event(key="2026x", year=2026, name="X", type=EventType.OFFSEASON, week=9)
    try:
        with model._sandbox(event):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert model.epas is real_epas
    assert model.counts is real_counts


def test_unknown_team_seeds_from_init_rating():
    model = mk_epa_model()
    model.enter_sandbox("2026cc")
    assert model.epas[9999].mean[0] == 0.0  # the defaultdict's init rating
    model.exit_sandbox()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && poetry run python -m pytest tests/test_offseason_sandbox_epa.py -q`
Expected: FAIL — `AttributeError: 'RecordingModel' object has no attribute 'enter_sandbox'` and `test_offseason_match_updates_rating` failing with `assert [] == [1, 2, 3, 4, 5, 6]`.

- [ ] **Step 3: Add the constant**

Append to `backend/src/models/epa/constants.py`:

```python
# Match count a team's rating carries into an offseason sandbox fork.
# percent_func spans 0.333 -> 0.200 and clamps at 12 matches, so 12 and a full
# season's count are the same setting. The 2026-07-30 backtest over 61 events /
# 3,294 matches measured 0 best on RMSE and Brier; no value removes the
# degraded-event tail, so this is a re-tuning hook, not a risk dial.
SANDBOX_SEED_COUNT = 0
```

- [ ] **Step 4: Add the sandbox seam to the base model**

In `backend/src/models/template.py`, add to the imports at the top:

```python
from contextlib import contextmanager
from typing import Any, Dict, Iterator, Optional, Tuple
```

Add these methods to `Model`, immediately after `post_record_team`:

```python
    def enter_sandbox(self, event_key: str) -> None:
        """Install per-event rating state so this event's matches evolve a fork
        of each team's rating. Subclasses carrying mutable per-team state
        override this; the base model has none."""
        return None

    def exit_sandbox(self) -> None:
        """Restore the real rating state installed before enter_sandbox."""
        return None

    @contextmanager
    def _sandbox(self, event: Event) -> Iterator[bool]:
        """Yields True when the event's matches run against sandbox state."""
        if event.type != EventType.OFFSEASON:
            yield False
            return
        self.enter_sandbox(event.key)
        try:
            yield True
        finally:
            self.exit_sandbox()
```

Replace the body of `process_match` (currently lines 54-101) with:

```python
    def process_match(
        self,
        match: Match,
        event: Event,
        team_events: Dict[int, TeamEvent],
        team_years: Dict[int, TeamYear],
    ):
        with self._sandbox(event) as sandboxed:
            win_prob, red_pred, blue_pred = self.predict_match(match, event)
            match_pred = MatchPred(win_prob, red_pred, blue_pred)

            pre_epas: Dict[str, Any] = {}
            for team in team_events.keys():
                pre_epas[str(team)] = self.pre_record_team(
                    team, team_events[team], team_years[team]
                )
            match.pre_epas = pre_epas

            self.record_match(match, match_pred)
            if match.status == MatchStatus.UPCOMING:
                return

            attributions = self.attribute_match(match, red_pred, blue_pred)

            # Don't update if 1) placeholder match, 2) elim dq, 3) all fouls.
            # Offseason matches DO update, but `_sandbox` has swapped in
            # event-scoped state, so they move only that event's fork.
            teams = set(match.get_red() + match.get_blue())
            placeholder_match = len(set(PLACEHOLDER_TEAMS).intersection(teams)) > 0
            elim_dq = match.elim and (
                len(match.get_red_dqs()) >= self.num_teams
                or len(match.get_blue_dqs()) >= self.num_teams
            )
            all_fouls = (
                match.blue_no_foul == 0
                and (match.blue_foul or 0) > 0
                and match.red_no_foul == 0
                and (match.red_foul or 0) > 0
            )
            skip_update = placeholder_match or elim_dq or all_fouls

            epas: Dict[str, Any] = {}
            for team, attr in attributions.items():
                if not skip_update:
                    self.update_team(team, attr, match)
                # A sandbox rating must never reach TeamYear: those fields feed
                # the year percentiles and next season's seed.
                epas[str(team)] = self.post_record_team(
                    team,
                    team_events[team],
                    None if sandboxed else team_years[team],
                )
            match.epas = epas
```

- [ ] **Step 5: Implement the fork in the EPA model**

In `backend/src/models/epa/main.py`, add to the constants import on line 12:

```python
from src.models.epa.constants import ELIM_WEIGHT, MEAN_REVERSION, SANDBOX_SEED_COUNT
```

Add these two classes above `class EPA(Model):`:

```python
class SandboxRatings(Dict[int, EPARating]):
    """Event-scoped ratings that fork from the real ones on first touch."""

    def __init__(self, base: Dict[int, EPARating]):
        super().__init__()
        self._base = base

    def __missing__(self, team: int) -> EPARating:
        forked = EPARating(np.array(self._base[team].mean, copy=True))
        self[team] = forked
        return forked


class SandboxCounts(Dict[int, int]):
    """Event-scoped match counts, seeded at SANDBOX_SEED_COUNT."""

    def __init__(self, seed: int):
        super().__init__()
        self._seed = seed

    def __missing__(self, team: int) -> int:
        self[team] = self._seed
        return self._seed
```

Add an `__init__` and the two hooks to `EPA`. Put `__init__` directly under `k: float`:

```python
    def __init__(self):
        super().__init__()
        self._sandboxes: Dict[str, Tuple[SandboxRatings, SandboxCounts]] = {}
        self._real_state: Optional[Tuple[Dict[int, EPARating], Dict[int, int]]] = None
```

Add the hooks after `start_season`:

```python
    def enter_sandbox(self, event_key: str) -> None:
        sandbox = self._sandboxes.get(event_key)
        if sandbox is None:
            # Built while self.epas is still the real dict, so the fork base is
            # the team's true rating -- never another event's sandbox.
            sandbox = (SandboxRatings(self.epas), SandboxCounts(SANDBOX_SEED_COUNT))
            self._sandboxes[event_key] = sandbox
        self._real_state = (self.epas, self.counts)
        self.epas, self.counts = sandbox

    def exit_sandbox(self) -> None:
        if self._real_state is None:
            return
        self.epas, self.counts = self._real_state
        self._real_state = None
```

Also add `_sandboxes` reset to `start_season`, immediately after `self.counts` is created:

```python
        self._sandboxes = {}
        self._real_state = None
```

Ensure `Tuple` and `Optional` are in the `typing` import on line 2 (they already are).

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && poetry run python -m pytest tests/test_offseason_sandbox_epa.py -q`
Expected: PASS, 8 tests.

- [ ] **Step 7: Run the whole suite for regressions**

Run: `cd backend && poetry run python -m pytest tests/ -q`
Expected: PASS, 178 tests (171 baseline − 2 deleted + 8 new, plus 1 net from the rename).

- [ ] **Step 8: Lint and type check**

Run: `cd backend && poetry run black src/ tests/ && poetry run isort src/ tests/ && poetry run flake8 src/ tests/`
Expected: no errors.

- [ ] **Step 9: Commit**

```bash
git add backend/src/models backend/tests
git commit -m "feat(offseason): fork EPA per offseason event

Offseason matches update an event-scoped copy of each team's rating rather
than being skipped. One swap of self.epas/self.counts in process_match covers
the whole model, so no EPA math changes. TeamYear is never stamped from a
sandbox rating."
```

---

### Task 2: Containment in the aggregation pass

**Files:**
- Modify: `backend/src/data/epa/agg.py:12-23` (`process_year_epas`), `:26-40` (`compact_from_match`), `:43-75` (`process_year` head), `:170-199` (TeamEvent loop)
- Test: `backend/tests/test_offseason_epa_containment.py`

**Interfaces:**
- Consumes: Task 1's sandbox fork, which makes `Match.epas` carry sandbox values for offseason matches.
- Produces: `process_year_epas(matches, team, default, offseason_events: Set[str])`; `compact_from_match(match, team, offseason_events: Set[str])` emitting an `offseason: bool` key. Task 3 reads that key.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_offseason_epa_containment.py`:

```python
from src.data.epa.agg import compact_from_match, process_year_epas
from src.db.models import Match
from src.types.enums import CompLevel, MatchStatus


def mk_match(key, event, week, time, team, post_epa):
    m = Match(
        key=key,
        year=2026,
        event=event,
        week=week,
        elim=False,
        comp_level=CompLevel.QUAL,
        set_number=1,
        match_number=1,
        time=time,
        status=MatchStatus.COMPLETED,
        red_1=team,
        red_2=2,
        red_3=3,
        blue_1=4,
        blue_2=5,
        blue_3=6,
        red_dq="",
        red_surrogate="",
        blue_dq="",
        blue_surrogate="",
    )
    m.pre_epas = {str(team): {"epa": post_epa}}
    m.epas = {str(team): {"epa": post_epa}}
    return m


def test_offseason_matches_excluded_from_season_epa():
    team = 254
    regular = mk_match("2026casj_qm1", "2026casj", 3, 100, team, 80.0)
    offseason = mk_match("2026cc_qm1", "2026cc", 9, 200, team, 150.0)

    pre_champs, end, max_ = process_year_epas(
        [regular, offseason], team, 0.0, {"2026cc"}
    )

    assert end == 80.0, "season EPA must ignore the offseason match"
    assert max_ == 80.0


def test_offseason_matches_included_when_not_flagged():
    """Guard against the filter silently matching nothing."""
    team = 254
    regular = mk_match("2026casj_qm1", "2026casj", 3, 100, team, 80.0)
    offseason = mk_match("2026cc_qm1", "2026cc", 9, 200, team, 150.0)

    _, end, _ = process_year_epas([regular, offseason], team, 0.0, set())
    assert end == 150.0


def test_compact_from_match_flags_offseason():
    team = 254
    regular = mk_match("2026casj_qm1", "2026casj", 3, 100, team, 80.0)
    offseason = mk_match("2026cc_qm1", "2026cc", 9, 200, team, 150.0)

    assert compact_from_match(regular, team, {"2026cc"})["offseason"] is False
    assert compact_from_match(offseason, team, {"2026cc"})["offseason"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && poetry run python -m pytest tests/test_offseason_epa_containment.py -q`
Expected: FAIL — `TypeError: process_year_epas() takes 3 positional arguments but 4 were given`.

- [ ] **Step 3: Thread the offseason key set through agg.py**

In `backend/src/data/epa/agg.py`, change the import line 3 to include `Set`:

```python
from typing import Any, Dict, List, Set, Tuple
```

Add `EventType` to the imports:

```python
from src.types.enums import EventType
```

Replace `process_year_epas` (lines 12-23):

```python
def process_year_epas(
    matches: List[Match], team: int, default: float, offseason_events: Set[str]
) -> Tuple[float, float, float]:
    # Offseason matches carry sandbox EPA, which must never reach season
    # aggregates -- these feed norm_epa, ranks, and next season's seed.
    matches = [m for m in matches if m.event not in offseason_events]
    post_epas = [(m.epas or {}).get(str(team), {}).get("epa") for m in matches]
    arr = [x for x in post_epas if x is not None]
    pre_champs_arr = [
        x for x, m in zip(post_epas, matches) if x is not None and m.week < 8
    ]
    pre_champs = pre_champs_arr[-1] if pre_champs_arr else default
    end = arr[-1] if arr else default
    max_ = max(arr[8:]) if len(arr) > 8 else end
    return pre_champs, end, max_
```

Replace `compact_from_match` (lines 26-40):

```python
def compact_from_match(
    match: Match, team: int, offseason_events: Set[str]
) -> Dict[str, Any]:
    pre = (match.pre_epas or {}).get(str(team), {})
    post = (match.epas or {}).get(str(team), {})
    alliance = "red" if team in match.get_red() else "blue"
    return {
        "match": match.key,
        "event": match.event,
        "alliance": alliance,
        "time": match.time,
        "week": match.week,
        "elim": match.elim,
        "status": match.status,
        "offseason": match.event in offseason_events,
        **pre,
        "post_epa": post.get("epa"),
    }
```

In `process_year`, build the set once. Insert immediately after `sd = year.score_sd or 0` (line 47):

```python
    # Single source of truth for "is this event offseason" across this pass.
    offseason_events = {
        e.key for e in objs[2].values() if e.type == EventType.OFFSEASON
    }
```

Update the two call sites in the TeamYear loop (lines 71-75):

```python
        ty.epa_pre_champs, ty.epa, ty.epa_max = process_year_epas(
            ms, ty.team, ty.epa_start, offseason_events
        )

        ty.team_matches = [compact_from_match(m, ty.team, offseason_events) for m in ms]
```

- [ ] **Step 4: Break the cross-event epa_start chain**

In the TeamEvent loop, replace line 192 (`curr_epas[te.team] = te.epa`) with:

```python
            # An offseason event's final sandbox EPA must not become the next
            # event's epa_start -- that would leak IRI into Chezy Champs.
            if te.event not in offseason_events:
                curr_epas[te.team] = te.epa  # for next event epa_start
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && poetry run python -m pytest tests/test_offseason_epa_containment.py -q`
Expected: PASS, 3 tests.

- [ ] **Step 6: Run the whole suite**

Run: `cd backend && poetry run python -m pytest tests/ -q`
Expected: PASS, all tests.

- [ ] **Step 7: Lint and type check**

Run: `cd backend && poetry run black src/ tests/ && poetry run isort src/ tests/ && poetry run flake8 src/ tests/ && poetry run pyright`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add backend/src/data/epa/agg.py backend/tests/test_offseason_epa_containment.py
git commit -m "fix(offseason): keep sandbox EPA out of season aggregates

Three leak paths close here: season epa/epa_max ignore offseason matches, an
offseason event no longer seeds the next event's epa_start, and team_matches
entries carry an explicit offseason flag for the frontend."
```

---

### Task 3: Frontend summary line and Figures filter

**Files:**
- Modify: `frontend/src/types/api.tsx:289-306` (`APITeamMatch`)
- Modify: `frontend/src/pagesContent/team/overview.tsx:114-191`
- Modify: `frontend/src/pagesContent/team/figures.tsx:17-21`

**Interfaces:**
- Consumes: Task 2's `offseason: boolean` on each `team_matches` entry.
- Produces: no downstream consumers.

- [ ] **Step 1: Add the field to the type**

In `frontend/src/types/api.tsx`, add to `APITeamMatch` after `elim: boolean;`:

```typescript
  offseason: boolean;
```

- [ ] **Step 2: Filter Figures**

Replace the body of `FigureSection` in `frontend/src/pagesContent/team/figures.tsx`:

```typescript
  // Offseason matches carry per-event sandbox EPA, which is not on the same
  // scale as the season line -- plotting it would imply a rating change that
  // never happened.
  const seasonMatches = matches.filter((match) => !match.offseason);

  return (
    <div className="w-full h-auto flex flex-col justify-center items-center px-2">
      <div className="w-full text-2xl font-bold mb-4">EPA Over Time</div>
      <TeamLineChart teamNum={teamNum} year={year} teamYear={teamYear} data={seasonMatches} />
    </div>
  );
```

- [ ] **Step 3: Add the summary line to the team overview**

In `frontend/src/pagesContent/team/overview.tsx`, add this derivation immediately after `const matches = teamYearData?.matches || [];` (line 67):

```typescript
  const offseasonEventKeys = new Set(
    matches.filter((match) => match.offseason).map((match) => match.event)
  );
  const offseasonEvents = teamEvents.filter((event) =>
    offseasonEventKeys.has(event.event)
  );
```

Then insert this block immediately **above** the horizontal rule `<div className="w-full h-1 bg-gray-300" />` (line 191):

```tsx
      {offseasonEvents.length > 0 && (
        <div className="w-full mb-4 text-sm">
          {offseasonEvents.map((event) => (
            <div key={event.event}>
              Offseason:{" "}
              <Link href={`/event/${event.event}`} className="text_link">
                {event.event_name}
              </Link>{" "}
              — EPA <strong>{event?.epa?.breakdown?.total_points?.toFixed(1)}</strong>{" "}
              <span className="text-gray-500">(does not affect season EPA)</span>
            </div>
          ))}
        </div>
      )}
```

`Link` is already imported at line 4.

- [ ] **Step 4: Type check and lint**

Run: `cd frontend && yarn lint && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git add frontend/src
git commit -m "feat(offseason): surface sandbox EPA on the team page

A labeled summary line above the match history names each offseason event and
its sandbox EPA; the event keeps its own block and match table below. Figures
excludes offseason matches so the season EPA line stays continuous."
```

---

### Task 4: Backtest harness

**Files:**
- Create: `backend/src/models/backtest.py`
- Test: `backend/tests/test_backtest_harness.py`

**Interfaces:**
- Consumes: Task 1's `EPA._sandboxes` and Task 2's containment.
- Produces: `bucket_of(index: int) -> str`; `Metrics` dataclass with fields `count, rmse, bias, acc, brier`; `score_metrics_for(matches: List[Match]) -> Metrics`; `run_year(year: int, sandbox: bool) -> Dict[str, List[Match]]`; `main()` CLI entry.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_backtest_harness.py`:

```python
from src.models.backtest import Metrics, bucket_of, score_metrics_for
from src.db.models import Match
from src.types.enums import CompLevel, MatchStatus, MatchWinner


def test_bucket_of():
    assert bucket_of(0) == "1-20"
    assert bucket_of(19) == "1-20"
    assert bucket_of(20) == "21-50"
    assert bucket_of(49) == "21-50"
    assert bucket_of(50) == "51+"


def mk_match(red_pred, blue_pred, red_actual, blue_actual, win_prob):
    m = Match(
        key="2026cc_qm1",
        year=2026,
        event="2026cc",
        week=9,
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
        red_score=red_actual,
        blue_score=blue_actual,
        red_no_foul=red_actual,
        blue_no_foul=blue_actual,
    )
    m.epa_red_score_pred = red_pred
    m.epa_blue_score_pred = blue_pred
    m.epa_win_prob = win_prob
    m.epa_winner = MatchWinner.RED if win_prob >= 0.5 else MatchWinner.BLUE
    return m


def test_score_metrics_for_perfect_prediction():
    m = mk_match(100.0, 50.0, 100, 50, 1.0)
    out = score_metrics_for([m])
    assert isinstance(out, Metrics)
    assert out.count == 1
    assert out.rmse == 0.0
    assert out.bias == 0.0
    assert out.acc == 1.0
    assert out.brier == 0.0


def test_score_metrics_for_biased_prediction():
    # predicts 10 high on both alliances, and calls the winner wrong
    m = mk_match(110.0, 60.0, 100, 50, 0.9)
    out = score_metrics_for([m])
    assert out.bias == -10.0  # actual - predicted
    assert out.rmse == 10.0
    assert out.acc == 1.0  # red predicted, red won
    assert abs(out.brier - 0.01) < 1e-9
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && poetry run python -m pytest tests/test_backtest_harness.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.models.backtest'`.

- [ ] **Step 3: Implement the harness**

Create `backend/src/models/backtest.py`:

```python
"""Offseason sandbox EPA backtest.

Replays a season's matches through the real EPA model twice -- once with the
per-event offseason sandbox active and once with it suppressed -- and reports
prediction quality for offseason matches only.

The gate this exists to serve: the sandbox must beat the frozen baseline on the
real 18-dimensional model before the feature ships. The scalar-offset proxy in
the design spec justified building this; it does not justify merging.

Usage:
    cd backend && poetry run python -m src.models.backtest 2025 2026
    cd backend && poetry run python -m src.models.backtest 2026 --sweep 0 6 12
"""

import argparse
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.data.epa.calc import process_year as process_year_calc
from src.db.models import Match
from src.google.snapshot import read_snapshot
from src.models import epa as epa_pkg  # noqa: F401  (keeps import order stable)
from src.models.epa import constants as epa_constants
from src.types.enums import EventType, MatchStatus, MatchWinner

EPS = 1e-9


@dataclass
class Metrics:
    count: int
    rmse: float
    bias: float
    acc: float
    brier: float


def bucket_of(index: int) -> str:
    """Within-event match index -> reporting bucket."""
    if index < 20:
        return "1-20"
    if index < 50:
        return "21-50"
    return "51+"


_OUTCOME = {MatchWinner.RED: 1.0, MatchWinner.BLUE: 0.0, MatchWinner.TIE: 0.5}


def score_metrics_for(matches: List[Match]) -> Metrics:
    """Alliance-level score error plus win-probability quality.

    bias is actual - predicted, so a negative bias means predictions run high.
    """
    residuals: List[float] = []
    briers: List[float] = []
    correct = 0
    counted = 0

    for match in matches:
        if match.epa_red_score_pred is None or match.epa_blue_score_pred is None:
            continue
        if match.red_score is None or match.blue_score is None:
            continue
        residuals.append(match.red_score - match.epa_red_score_pred)
        residuals.append(match.blue_score - match.epa_blue_score_pred)

        actual = _OUTCOME.get(match.get_winner())
        if actual is None or match.epa_win_prob is None:
            continue
        counted += 1
        briers.append((match.epa_win_prob - actual) ** 2)
        if (match.epa_win_prob >= 0.5) == (actual >= 0.5) or actual == 0.5:
            correct += 1

    if not residuals:
        return Metrics(0, 0.0, 0.0, 0.0, 0.0)

    rmse = math.sqrt(sum(x * x for x in residuals) / len(residuals))
    bias = sum(residuals) / len(residuals)
    acc = correct / counted if counted else 0.0
    brier = sum(briers) / len(briers) if briers else 0.0
    return Metrics(counted, rmse, bias, acc, brier)


def run_year(year: int, sandbox: bool) -> Tuple[Dict[str, List[Match]], List[str]]:
    """Replay a year through the real model. Returns offseason matches grouped
    by event key, in chronological order, plus the offseason event keys."""
    loaded = read_snapshot(year)
    if loaded is None:
        raise SystemExit(f"no snapshot for {year}; run the pipeline first")
    objs, _teams = loaded

    original = getattr(_SandboxSwitch, "enabled")
    _SandboxSwitch.enabled = sandbox
    try:
        objs = process_year_calc(objs, {})
    finally:
        _SandboxSwitch.enabled = original

    offseason_keys = [
        e.key for e in objs[2].values() if e.type == EventType.OFFSEASON
    ]
    by_event: Dict[str, List[Match]] = defaultdict(list)
    for match in objs[4].values():
        if match.event in offseason_keys and match.status == MatchStatus.COMPLETED:
            by_event[match.event].append(match)
    for key in by_event:
        by_event[key].sort(key=lambda m: (m.time, m.key))
    return by_event, offseason_keys


class _SandboxSwitch:
    """Lets the harness suppress the fork without editing the model."""

    enabled = True


def _install_switch() -> None:
    """Patch Model._sandbox so `enabled = False` reproduces frozen behavior:
    no fork, and no updates for offseason matches."""
    from contextlib import contextmanager

    from src.models.template import Model

    original = Model._sandbox

    @contextmanager
    def switched(self, event):
        if not _SandboxSwitch.enabled and event.type == EventType.OFFSEASON:
            # Frozen baseline: predict, record, but never move a rating.
            saved = self.update_team
            self.update_team = lambda *a, **k: None  # type: ignore[assignment]
            try:
                yield True
            finally:
                self.update_team = saved  # type: ignore[assignment]
            return
        with original(self, event) as sandboxed:
            yield sandboxed

    Model._sandbox = switched  # type: ignore[assignment]


def report(label: str, by_event: Dict[str, List[Match]]) -> Dict[str, Metrics]:
    buckets: Dict[str, List[Match]] = defaultdict(list)
    quals: List[Match] = []
    elims: List[Match] = []
    every: List[Match] = []
    for matches in by_event.values():
        for i, match in enumerate(matches):
            buckets[bucket_of(i)].append(match)
            every.append(match)
            (elims if match.elim else quals).append(match)

    out: Dict[str, Metrics] = {"ALL": score_metrics_for(every)}
    out["QUALS"] = score_metrics_for(quals)
    out["ELIMS"] = score_metrics_for(elims)
    for name in ("1-20", "21-50", "51+"):
        out[name] = score_metrics_for(buckets.get(name, []))

    print(f"  {label}")
    for name, m in out.items():
        if m.count == 0:
            continue
        print(
            f"    {name:>6}  n={m.count:5}  rmse={m.rmse:8.2f}  "
            f"bias={m.bias:+8.2f}  acc={m.acc:.4f}  brier={m.brier:.4f}"
        )
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("years", nargs="+", type=int)
    parser.add_argument(
        "--sweep",
        nargs="*",
        type=int,
        default=None,
        help="SANDBOX_SEED_COUNT values to try (default: just the shipped value)",
    )
    args = parser.parse_args(argv)

    _install_switch()

    for year in args.years:
        print(f"===== {year} =====")
        frozen, keys = run_year(year, sandbox=False)
        print(f"  {len(keys)} offseason events")
        base = report("FROZEN (ships today)", frozen)

        seeds = args.sweep if args.sweep else [epa_constants.SANDBOX_SEED_COUNT]
        for seed in seeds:
            epa_constants.SANDBOX_SEED_COUNT = seed
            import src.models.epa.main as epa_main

            epa_main.SANDBOX_SEED_COUNT = seed
            live, _ = run_year(year, sandbox=True)
            new = report(f"SANDBOX (SANDBOX_SEED_COUNT={seed})", live)
            print(
                f"    -> acc {new['ALL'].acc - base['ALL'].acc:+.4f}  "
                f"brier {base['ALL'].brier - new['ALL'].brier:+.4f}  "
                f"rmse {base['ALL'].rmse - new['ALL'].rmse:+.2f}"
            )
            if "ELIMS" in new and new["ELIMS"].count:
                print(
                    f"    -> elim acc {new['ELIMS'].acc - base['ELIMS'].acc:+.4f}  "
                    f"elim brier {base['ELIMS'].brier - new['ELIMS'].brier:+.4f}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && poetry run python -m pytest tests/test_backtest_harness.py -q`
Expected: PASS, 4 tests.

- [ ] **Step 5: Run the whole suite, lint, type check**

Run: `cd backend && poetry run python -m pytest tests/ -q && poetry run black src/ tests/ && poetry run isort src/ tests/ && poetry run flake8 src/ tests/`
Expected: all pass, no lint errors.

- [ ] **Step 6: Commit**

```bash
git add backend/src/models/backtest.py backend/tests/test_backtest_harness.py
git commit -m "test(offseason): add sandbox-vs-frozen backtest harness

Replays a season through the real EPA model with the fork on and off and
reports offseason prediction quality by quals/elims and match-index bucket.
This is the merge gate the design spec requires."
```

---

### Task 5: Local verification on real data

**Files:**
- Create: `backend/scripts/verify_offseason_isolation.py`

**Interfaces:**
- Consumes: Tasks 1, 2, and 4.
- Produces: an exit code — 0 when season EPA is unchanged for every year.

- [ ] **Step 1: Write the isolation verifier**

Create `backend/scripts/verify_offseason_isolation.py`:

```python
"""Prove the offseason sandbox changes no season rating.

Loads each year's snapshot, runs the EPA calc pass twice -- once with the fork
suppressed (frozen, the pre-change behavior) and once with it live -- and
compares every TeamYear rating field. Any difference is a containment bug.

Usage:
    cd backend && poetry run python scripts/verify_offseason_isolation.py 2025 2026
"""

import sys
from typing import Any, Dict, List

from src.data.epa.calc import process_year as process_year_calc
from src.google.snapshot import read_snapshot
from src.models.backtest import _SandboxSwitch, _install_switch

FIELDS = [
    "epa",
    "epa_start",
    "epa_pre_champs",
    "epa_max",
    "unitless_epa",
    "norm_epa",
    "auto_epa",
    "teleop_epa",
    "endgame_epa",
    "rp_1_epa",
    "rp_2_epa",
    "rp_3_epa",
    "tiebreaker_epa",
    "total_epa_rank",
    "total_epa_percentile",
    "country_epa_rank",
    "state_epa_rank",
    "district_epa_rank",
] + [f"comp_{i}_epa" for i in range(10)]


def snapshot_team_years(year: int, sandbox: bool) -> Dict[str, Dict[str, Any]]:
    loaded = read_snapshot(year)
    if loaded is None:
        raise SystemExit(f"no snapshot for {year}")
    objs, _ = loaded

    _SandboxSwitch.enabled = sandbox
    try:
        objs = process_year_calc(objs, {})
    finally:
        _SandboxSwitch.enabled = True

    return {
        key: {f: getattr(ty, f, None) for f in FIELDS}
        for key, ty in objs[1].items()
    }


def main(years: List[int]) -> int:
    _install_switch()
    failures = 0
    for year in years:
        frozen = snapshot_team_years(year, sandbox=False)
        live = snapshot_team_years(year, sandbox=True)

        assert set(frozen) == set(live), f"{year}: team-year key sets diverged"

        diffs = []
        for key in frozen:
            for field in FIELDS:
                a, b = frozen[key][field], live[key][field]
                if a != b:
                    diffs.append(f"{key}.{field}: {a!r} != {b!r}")

        if diffs:
            failures += 1
            print(f"FAIL {year}: {len(diffs)} differing TeamYear fields")
            for line in diffs[:20]:
                print(f"  {line}")
        else:
            print(f"OK   {year}: {len(frozen)} team-years bit-identical")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main([int(a) for a in sys.argv[1:]] or [2025, 2026]))
```

- [ ] **Step 2: Fetch real data into a snapshot**

The verifier and harness both read the GCS state snapshot. Confirm credentials and snapshot availability:

```bash
cd backend && poetry run python -c "
from src.google.snapshot import read_snapshot
for y in (2025, 2026):
    out = read_snapshot(y)
    print(y, 'ok' if out else 'MISSING', len(out[0][4]) if out else 0, 'matches')
"
```

Expected: both years `ok` with tens of thousands of matches. If MISSING, set `PROD=True` in the environment so `src/constants.py` selects the production bucket.

- [ ] **Step 3: Run the isolation verifier**

Run: `cd backend && PROD=True poetry run python scripts/verify_offseason_isolation.py 2025 2026`
Expected: `OK 2025: ... bit-identical` and `OK 2026: ... bit-identical`.

This is the spec §8 cross-season and within-season invariant. A `FAIL` here blocks the deploy.

- [ ] **Step 4: Run the accuracy gate**

Run: `cd backend && PROD=True poetry run python -m src.models.backtest 2025 2026`
Expected: for each year, the SANDBOX rows show higher `acc` and lower `brier` than FROZEN, especially on the `ELIMS` line.

Record the numbers. If sandbox loses, stop and reopen the design per spec §9 step 2.

- [ ] **Step 5: Commit**

```bash
git add backend/scripts/verify_offseason_isolation.py
git commit -m "test(offseason): add season-EPA isolation verifier

Runs the EPA pass with the fork suppressed and live, then diffs every TeamYear
rating field. Proves a 2025 offseason match cannot move a 2026 rating, which is
the one path (all_team_years -> get_init_epa) that could leak across seasons."
```

---

### Task 6: Ship and verify in production

**Files:** none — this task runs the deploy and verification procedure.

**Interfaces:**
- Consumes: Tasks 1-5, all committed and green.

- [ ] **Step 1: Read the deploy documentation**

Read `docs/superpowers/rig/deploy/DEPLOY.md` in full, and build the §2 pre-deploy checklist as todos. Do not hand-roll `gcloud` commands.

- [ ] **Step 2: Open, verify, and merge the PR**

```bash
git push -u fork feat/offseason-sandbox-epa
gh pr create --repo chondl/statbotics --base cph-staging --head feat/offseason-sandbox-epa \
  --title "feat(offseason): per-event sandbox EPA" --body "<summary + backtest numbers>"
gh pr merge <n> --repo chondl/statbotics --merge --delete-branch
```

Note: `origin` is the upstream repo and is push-disabled. Always push to `fork`.

- [ ] **Step 3: Deploy**

```bash
cd docs/superpowers/rig/deploy && make ship
```

`make ship` builds both images and rolls both services. Never deploy a single service.

- [ ] **Step 4: Trigger a full recompute**

```bash
cd docs/superpowers/rig/deploy && make reprocess-curr-year
```

Then repeat for 2025 with `make reprocess-year YEAR=2025` so the 2025 offseason back catalog republishes.

- [ ] **Step 5: Verify predictions changed for offseason events**

```bash
curl -s https://api-statbotics.iterativerefinement.com/v3/event/2026iri | python3 -m json.tool | head -40
```

Expected: the `metrics.score_pred.error` figure moves substantially toward zero from its current 50.06, and `metrics.win_prob.acc` is at or above the current 0.7778.

- [ ] **Step 6: Verify season EPA did not change**

Capture a sample of team-year EPAs before the deploy and compare after:

```bash
for t in 254 1114 2056 6238; do
  curl -s "https://api-statbotics.iterativerefinement.com/v3/team_year/$t/2026" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(d['team'], d['epa']['breakdown']['total_points'], d['epa']['norm'], d['epa']['ranks']['total']['rank'])"
done
```

Expected: identical to the pre-deploy capture. Any difference is a containment failure.

- [ ] **Step 7: Verify old matches did not change**

```bash
curl -s "https://api-statbotics.iterativerefinement.com/v3/matches?event=2026casj&limit=5" \
  | python3 -c "import json,sys; [print(m['key'], m['pred']['red_score'], m['pred']['blue_score'], m['pred']['red_win_prob']) for m in json.load(sys.stdin)]"
```

Expected: identical to the pre-deploy capture for this regular-season event.

- [ ] **Step 8: Verify the UI**

Load these in a real browser and confirm rendering:
- `https://statbotics.iterativerefinement.com/event/2026iri` — team table shows sandbox EPA, match predictions present, simulation tab runs.
- `https://statbotics.iterativerefinement.com/team/6238/2026` — offseason summary line renders above the horizontal rule; the offseason event still has its own block and match table below.
- The team's Figures tab — the EPA-over-time line has no offseason points.

- [ ] **Step 9: Record evidence and exit the worktree**

Write the observed numbers into the PR or a follow-up comment, then leave the worktree.

---

## Self-Review

**Spec coverage:**
- §4 fork → Task 1. §5 containment items 1-4 → Task 1 Step 4 (`ty=None`) and Task 2 Steps 3-4. §6 frontend → Task 3. §7 harness → Task 4. §8 verification → Task 1 tests, Task 2 tests, Task 5, Task 6 Steps 5-8. §9 rollout → Task 6.
- §5's note that `te.epa_start` needs no change is covered by leaving `agg.py:188` untouched.
- §8's `2026isrtp` case is covered by the existing `tests/test_offseason_ingest.py`, which Task 1 Step 7 re-runs.

**Placeholder scan:** the only bracketed placeholder is the PR body in Task 6 Step 2, which is filled from the recorded backtest numbers produced in Task 5 Step 4.

**Type consistency:** `process_year_epas` and `compact_from_match` take `offseason_events: Set[str]` in both the definition and both call sites. `Metrics` fields are used identically in `score_metrics_for`, `report`, and the tests. `_SandboxSwitch` and `_install_switch` are defined in Task 4 and imported by Task 5.
