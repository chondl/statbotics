"""Fetch-layer hygiene tests (TBA cache design §3 items 1, 2, 4, 5).

Covers: per-request conditional headers, the check_year_partial alliances
ETag lookup, the TBA_CACHE_DIR env var, and dump_cache OSError logging.
"""

import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import src.data.tba as data_tba
import src.tba.main as tba_main
from src.db.models import ETag
from src.tba.utils import dump_cache
from src.types.enums import EventStatus


class FakeResponse:
    def __init__(self, status_code, json_data=None, headers=None):
        self.status_code = status_code
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        return self._json


class FakeSession:
    """Captures per-call headers and mimics requests.Session.get."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def get(self, url, headers=None, **kwargs):
        self.calls.append((url, dict(headers or {})))
        return self.responses.pop(0)


"""
1. Per-request conditional headers
"""


def test_etag_not_leaked_to_later_requests(monkeypatch):
    fake = FakeSession(
        [
            FakeResponse(304),
            FakeResponse(200, json_data={"a": 1}, headers={"ETag": 'W/"new"'}),
        ]
    )
    monkeypatch.setattr(tba_main, "session", fake)

    # First call: conditional GET that 304s.
    data, etag = tba_main._get_tba("event/2026iri/matches", etag='W/"old"')
    assert data is True and etag == 'W/"old"'
    assert fake.calls[0][1].get("If-None-Match") == 'W/"old"'

    # Second call with etag=None must NOT carry If-None-Match.
    data, etag = tba_main._get_tba("events/2026", etag=None)
    assert data == {"a": 1} and etag == 'W/"new"'
    assert "If-None-Match" not in fake.calls[1][1]

    # And the shared session's own headers must never be mutated.
    assert "If-None-Match" not in fake.headers


"""
2. check_year_partial alliances ETag lookup
"""


def _mk_event(key):
    today = datetime.now()
    return SimpleNamespace(
        key=key,
        time=0,
        status=EventStatus.ONGOING,
        start_date=(today - timedelta(days=1)).strftime("%Y-%m-%d"),
        end_date=(today + timedelta(days=1)).strftime("%Y-%m-%d"),
        qual_matches=10,
        current_match=10,
    )


def test_check_year_partial_alliances_uses_alliances_etag(monkeypatch):
    key = "2026iri"
    etags = [
        ETag(2026, "2026/events", "E-events"),
        ETag(2026, key + "/matches", "E-matches"),
        ETag(2026, key + "/rankings", "E-rankings"),
        ETag(2026, key + "/alliances", "E-alliances"),
    ]
    seen = {}

    def fake_events(year, etag=None, cache=True):
        seen["events"] = etag
        return [], etag

    def fake_matches(year, event, time, etag=None, cache=True):
        seen["matches"] = etag
        return [], etag

    def fake_rankings(event, etag=None, cache=True):
        seen["rankings"] = etag
        return {}, etag

    def fake_alliances(event, etag=None, cache=True):
        seen["alliances"] = etag
        return ({}, {}), etag

    monkeypatch.setattr(data_tba, "get_events_tba", fake_events)
    monkeypatch.setattr(data_tba, "get_event_matches_tba", fake_matches)
    monkeypatch.setattr(data_tba, "get_event_rankings_tba", fake_rankings)
    monkeypatch.setattr(data_tba, "get_event_alliances_tba", fake_alliances)

    changed = data_tba.check_year_partial(2026, [_mk_event(key)], etags)

    assert changed is False  # all ETags matched
    assert seen["events"] == "E-events"
    assert seen["matches"] == "E-matches"
    assert seen["rankings"] == "E-rankings"
    assert seen["alliances"] == "E-alliances"


"""
3. TBA_CACHE_DIR env var
"""


def test_cache_dir_env_var_honored(tmp_path, monkeypatch):
    monkeypatch.setenv("TBA_CACHE_DIR", str(tmp_path))
    importlib.reload(tba_main)
    try:
        monkeypatch.setattr(
            tba_main, "_get_tba", lambda url, etag=None: ({"x": 1}, 'W/"e"')
        )
        data, _ = tba_main.get_tba("events/2026", cache=True)
        assert data == {"x": 1}
        assert (tmp_path / "events" / "2026" / "data.p").exists()

        # Second call must be served from the cache, not the network.
        def boom(url, etag=None):
            raise AssertionError("network hit on cache hit")

        monkeypatch.setattr(tba_main, "_get_tba", boom)
        data, etag = tba_main.get_tba("events/2026", cache=True)
        assert data == {"x": 1} and etag is None
    finally:
        monkeypatch.delenv("TBA_CACHE_DIR")
        importlib.reload(tba_main)


"""
4. dump_cache logs OSError
"""


def test_dump_cache_logs_oserror(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    bad_path = str(blocker / "sub")

    dump_cache(bad_path, {"x": 1})  # must not raise

    out = capsys.readouterr().out
    assert bad_path in out
