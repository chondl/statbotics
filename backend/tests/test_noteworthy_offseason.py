"""Noteworthy-match leaderboards include offseason events only on the
Offseason (week 9) leaderboard; every other view excludes them."""

from src.db_duckdb.main import OFFSEASON_WEEK, _noteworthy_filters
from src.types.enums import EventType


def _excludes_offseason(where, params):
    return "e.type != ?" in where and EventType.OFFSEASON.value in params


def test_all_weeks_excludes_offseason():
    where, params = _noteworthy_filters(2026, None, None, None, None, None)
    assert _excludes_offseason(where, params)
    assert "e.week = ?" not in where


def test_season_week_excludes_offseason():
    where, params = _noteworthy_filters(2026, None, None, None, None, 3)
    assert _excludes_offseason(where, params)
    assert "e.week = ?" in where


def test_offseason_week_includes_offseason():
    where, params = _noteworthy_filters(2026, None, None, None, None, OFFSEASON_WEEK)
    assert not _excludes_offseason(where, params)
    assert where[-1] == "e.week = ?" and params[-1] == OFFSEASON_WEEK


def test_params_line_up_with_placeholders():
    where, params = _noteworthy_filters(2026, "USA", "CA", "regionals", True, 3)
    assert sum(w.count("?") for w in where) == len(params)
    where, params = _noteworthy_filters(2026, "USA", "CA", "fim", False, 9)
    assert sum(w.count("?") for w in where) == len(params)
