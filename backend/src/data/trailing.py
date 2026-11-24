"""Trailing-year window rule and live-year predicate.

Trailing year = CURR_YEAR - 1. The trailing window is open until Dec 28 00:00
America/Los_Angeles of the trailing year (2026-12-28 08:00 UTC for 2026); from
then on the trailing year is an ordinary past year. A live year is CURR_YEAR,
or the trailing year while the window is open.

CURR_YEAR is read from `src.constants` at call time, so tests pin the year by
monkeypatching `src.constants.CURR_YEAR` (not a local copy in this module).

Classification of every CURR_YEAR use in backend/src
(from `grep -rn CURR_YEAR backend/src`):

Live-year (must use is_live_year, not `== CURR_YEAR`):
  - src/data/tba.py:92    (get_event_status: Upcoming/Ongoing only when live)
  - src/data/tba.py:303   (full_curr_year in the TBA fetch)
  - src/data/router.py:136 (event-key pattern accepted by the event ping)

Current-year-only (stay as they are):
  - src/breakdown.py:3,224
  - src/tba/breakdown.py:4,7
  - src/api/query.py:3,55
  - src/db/models/team_year.py:8,178
  - src/data/router.py:7,83,97
  - src/data/main.py:7,73,217,230,236,238,240,241,255,268,271,292,338,446,451,526
  - src/data/tba.py:4
  - src/data/wins.py:5,187
  - src/data/sweep.py:28,39 (and the docstring at 4,47)
  - src/data/teams.py:11,47,48,54 (47 and 48 are docstring)
  - src/data/epa/main.py:4,43
  - src/google/storage.py:12,175,188,192,204,365
  - src/site/team_year.py:7,22
  - src/db_duckdb/main.py:11,535,563,564
"""

from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import src.constants as constants

PACIFIC = ZoneInfo("America/Los_Angeles")


def trailing_year() -> int:
    return constants.CURR_YEAR - 1


def _window_open_at(now: datetime) -> bool:
    close = datetime(trailing_year(), 12, 28, tzinfo=PACIFIC)
    return now < close


def _is_live_year_at(year: int, now: datetime) -> bool:
    if year == constants.CURR_YEAR:
        return True
    return year == trailing_year() and _window_open_at(now)


def _now(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def trailing_window_open(now: Optional[datetime] = None) -> bool:
    return _window_open_at(_now(now))


def is_live_year(year: int, now: Optional[datetime] = None) -> bool:
    if year == constants.CURR_YEAR:
        return True
    return year == trailing_year() and trailing_window_open(now)
