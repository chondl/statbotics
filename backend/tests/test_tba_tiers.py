"""Freshness-tier policy tests (TBA cache design §2.3).

Covers: the active-window math (start−1d .. end+3d, naive date-string
compare) and the widened check_year_partial probe window. No real TBA or
GCS access.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import src.data.tba as data_tba
from src.constants import CURR_YEAR
from src.types.enums import EventStatus

"""
1. Active-window math (design §2.3): start−1d .. end+3d
"""


def test_in_event_window_boundaries():
    in_window = data_tba.in_event_window
    start, end = "2026-07-01", "2026-07-04"
    assert not in_window(start, end, "2026-06-29")  # start−2d: out
    assert in_window(start, end, "2026-06-30")  # start−1d: in
    assert in_window(start, end, "2026-07-02")  # mid-event: in
    assert in_window(start, end, "2026-07-07")  # end+3d: in (grace)
    assert not in_window(start, end, "2026-07-08")  # end+4d: out


def test_in_event_window_string_compare_survives_month_rollover():
    # end+3d crosses a month boundary; the compare stays a naive date-string
    # compare (pre-tier semantics), with the rollover handled by date math.
    assert data_tba.in_event_window("2026-06-25", "2026-06-29", "2026-07-02")
    assert not data_tba.in_event_window("2026-06-25", "2026-06-29", "2026-07-03")


"""
2. check_year_partial probe window widens to end+3d (etag semantics unchanged)
"""


def _probe_event(key, end_offset_days):
    today = datetime.now()
    return SimpleNamespace(
        key=key,
        time=0,
        status=EventStatus.ONGOING,
        start_date=(today + timedelta(days=end_offset_days - 3)).strftime("%Y-%m-%d"),
        end_date=(today + timedelta(days=end_offset_days)).strftime("%Y-%m-%d"),
        qual_matches=0,
        current_match=0,
    )


def _patch_probe_fakes(monkeypatch, calls):
    def fake_events(year, etag=None, cache=True):
        calls.append(("events", etag))
        return [], etag  # unchanged

    def fake_matches(year, event, time, etag=None, cache=True):
        calls.append(("matches:" + event, etag))
        return [], 'W/"changed"'

    monkeypatch.setattr(data_tba, "get_events_tba", fake_events)
    monkeypatch.setattr(data_tba, "get_event_matches_tba", fake_matches)


def test_check_year_partial_probes_through_end_grace(monkeypatch):
    """An event that ended 2 days ago is still inside the +3d grace window,
    so its matches etag is probed."""
    calls = []
    _patch_probe_fakes(monkeypatch, calls)
    event = _probe_event("2026aaa", end_offset_days=-2)

    changed = data_tba.check_year_partial(CURR_YEAR, [event], [])

    assert changed is True  # the changed matches etag was seen
    assert ("matches:2026aaa", "NA") in calls


def test_check_year_partial_skips_past_end_grace(monkeypatch):
    calls = []
    _patch_probe_fakes(monkeypatch, calls)
    event = _probe_event("2026bbb", end_offset_days=-4)

    changed = data_tba.check_year_partial(CURR_YEAR, [event], [])

    assert changed is False
    assert calls == [("events", "NA")]  # no per-event probes
