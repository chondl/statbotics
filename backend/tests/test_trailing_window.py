from datetime import datetime, timezone

import pytest

import src.constants as constants
from src.data import trailing


@pytest.fixture(autouse=True)
def _year_2027(monkeypatch):
    monkeypatch.setattr(constants, "CURR_YEAR", 2027)


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


def test_trailing_year_reads_constants_at_call_time(monkeypatch):
    assert trailing.trailing_year() == 2026
    monkeypatch.setattr(constants, "CURR_YEAR", 2030)
    assert trailing.trailing_year() == 2029


def test_open_before_close():
    assert trailing._window_open_at(utc(2026, 12, 28, 7, 59, 59)) is True


def test_closed_at_close():
    assert trailing._window_open_at(utc(2026, 12, 28, 8, 0, 0)) is False


def test_open_at_rollover():
    assert trailing._window_open_at(utc(2026, 11, 6, 7, 0, 0)) is True


def test_live_years():
    now = utc(2026, 12, 1)
    assert trailing._is_live_year_at(2027, now) is True
    assert trailing._is_live_year_at(2026, now) is True
    assert trailing._is_live_year_at(2025, now) is False
    after = utc(2026, 12, 28, 8, 0, 0)
    assert trailing._is_live_year_at(2027, after) is True
    assert trailing._is_live_year_at(2026, after) is False
    assert trailing._is_live_year_at(2025, after) is False


def test_conftest_pins_window_closed():
    assert trailing.trailing_window_open() is False
    assert trailing.is_live_year(2026) is False
    assert trailing.is_live_year(2027) is True


def test_trailing_open_fixture(trailing_open):
    assert trailing.trailing_window_open() is True
    assert trailing.is_live_year(2026) is True
    assert trailing.is_live_year(2025) is False
