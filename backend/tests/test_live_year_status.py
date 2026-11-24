from datetime import datetime

import src.data.tba as data_tba
from src.constants import CURR_YEAR
from src.data.trailing import trailing_year
from src.db.models import Event
from src.types.enums import CompLevel, EventStatus, MatchStatus, MatchWinner


def qual_match():
    return {
        "comp_level": CompLevel.QUAL,
        "winner": MatchWinner.RED,
        "status": MatchStatus.COMPLETED,
    }


def test_trailing_event_without_matches_is_upcoming(trailing_open):
    assert data_tba.get_event_status([], trailing_year()) == EventStatus.UPCOMING


def test_trailing_event_with_quals_only_is_ongoing(trailing_open):
    status = data_tba.get_event_status([qual_match()], trailing_year())
    assert status == EventStatus.ONGOING


def test_past_year_event_without_matches_is_invalid():
    # window closed (autouse fixture): the trailing year is an ordinary past year
    assert data_tba.get_event_status([], trailing_year()) == EventStatus.INVALID


def test_check_year_partial_probes_ongoing_trailing_event(monkeypatch, trailing_open):
    year = trailing_year()
    today = datetime.now().strftime("%Y-%m-%d")
    event = Event(
        key=f"{year}test",
        year=year,
        name="Test",
        time=0,
        country="USA",
        state="WA",
        district=None,
        start_date=today,
        end_date=today,
        type=0,
        week=1,
        video=None,
        status=EventStatus.ONGOING,
    )
    monkeypatch.setattr(data_tba, "get_events_tba", lambda *a, **k: ([], "same"))
    monkeypatch.setattr(
        data_tba, "get_event_matches_tba", lambda *a, **k: ([], "new-etag")
    )
    monkeypatch.setattr(data_tba, "get_raw_offseason_events_tba", lambda y: [])
    etags = [
        data_tba.ETag(year, f"{year}/events", "same"),
        data_tba.ETag(year, f"{event.key}/matches", "old-etag"),
    ]
    assert data_tba.check_year_partial(year, [event], etags) is True
