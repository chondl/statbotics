"""Freshness-tier policy tests (TBA cache design §2.3).

Covers: the active-window math (start−1d .. end+3d, naive date-string
compare), the widened check_year_partial probe window, the daily
revalidation tier in partial cycles, trust-cache semantics for full
current-year rebuilds, and the REFRESH_TBA force flag. No real TBA or GCS
access.
"""

import os
import pickle
from datetime import datetime, timedelta
from types import SimpleNamespace

import src.data.main as data_main
import src.data.tba as data_tba
import src.tba.cache as tba_cache
import src.tba.main as tba_main
from src.constants import CURR_YEAR
from src.db.models import ETag, Event, Year
from src.tba.types import empty_breakdown
from src.types.enums import (
    CompLevel,
    EventStatus,
    EventType,
    MatchStatus,
    MatchWinner,
)

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
    def fake_events(year, etag=None, cache=True, tier_probes=False):
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


"""
Shared process_year scaffolding for tier tests
"""


def _event_obj(key, start_offset_days, end_offset_days, status, year=CURR_YEAR):
    today = datetime.now()
    return Event(
        key=key,
        year=year,
        name="Event " + key,
        time=0,
        country=None,
        state=None,
        district=None,
        start_date=(today + timedelta(days=start_offset_days)).strftime("%Y-%m-%d"),
        end_date=(today + timedelta(days=end_offset_days)).strftime("%Y-%m-%d"),
        type=EventType.OFFSEASON,
        week=9,
        video=None,
        status=status,
    )


def _objs(event, etags=()):
    etags_dict = {e.path: e for e in etags}
    # objs_type: (year, team_years, events, team_events, matches, etags)
    return (Year(year=event.year), {}, {event.key: event}, {}, {}, etags_dict)


def _match_dict(event_key, match_key, comp_level=CompLevel.QUAL):
    breakdown = dict(empty_breakdown)
    return {
        "event": event_key,
        "key": match_key,
        "comp_level": comp_level,
        "set_number": 1,
        "match_number": 1,
        "status": MatchStatus.COMPLETED,
        "video": None,
        "red_1": 1,
        "red_2": 2,
        "red_3": 3,
        "red_dq": "",
        "red_surrogate": "",
        "blue_1": 4,
        "blue_2": 5,
        "blue_3": 6,
        "blue_dq": "",
        "blue_surrogate": "",
        "winner": MatchWinner.RED,
        "time": 0,
        "predicted_time": None,
        "red_score": 10,
        "blue_score": 5,
        "red_score_breakdown": breakdown,
        "blue_score_breakdown": breakdown,
    }


def _completed_matches(event_key):
    # One qual + one final: elims started, no upcoming -> COMPLETED status.
    return [
        _match_dict(event_key, event_key + "_qm1"),
        _match_dict(event_key, event_key + "_f1m1", CompLevel.FINAL),
    ]


class RecordingTBA:
    """Monkeypatches the read_tba functions used by process_year and records
    every (name, etag, cache) call."""

    def __init__(self, monkeypatch, matches_by_event=None):
        self.calls = []
        self.matches_by_event = matches_by_event or {}

        def districts(year, etag=None, cache=True):
            self.calls.append(("districts", etag, cache))
            return [], None

        def events(year, etag=None, cache=True, tier_probes=False):
            self.calls.append(("events", etag, cache))
            return [], None

        def matches(year, event, time, etag=None, cache=True):
            self.calls.append(("matches:" + event, etag, cache))
            return self.matches_by_event.get(event, []), 'W/"m-' + event + '"'

        def teams(event, etag=None, cache=True):
            self.calls.append(("teams:" + event, etag, cache))
            return [], None

        def rankings(event, etag=None, cache=True):
            self.calls.append(("rankings:" + event, etag, cache))
            return {}, 'W/"r-' + event + '"'

        def alliances(event, etag=None, cache=True):
            self.calls.append(("alliances:" + event, etag, cache))
            return ({}, {}), 'W/"a-' + event + '"'

        monkeypatch.setattr(data_tba, "get_districts_tba", districts)
        monkeypatch.setattr(data_tba, "get_events_tba", events)
        monkeypatch.setattr(data_tba, "get_event_matches_tba", matches)
        monkeypatch.setattr(data_tba, "get_event_teams_tba", teams)
        monkeypatch.setattr(data_tba, "get_event_rankings_tba", rankings)
        monkeypatch.setattr(data_tba, "get_event_alliances_tba", alliances)

    def named(self, name):
        return [c for c in self.calls if c[0] == name]


def _set_manifest(url, etag, age_hours):
    ts = datetime.utcnow() - timedelta(hours=age_hours)
    tba_cache._manifest[url] = {
        "etag": etag,
        "last_validated": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _seed_event_manifest(key, age_hours):
    _set_manifest("event/" + key + "/matches", 'W/"m-' + key + '"', age_hours)
    _set_manifest("event/" + key + "/rankings", 'W/"r-' + key + '"', age_hours)
    _set_manifest("event/" + key + "/alliances", 'W/"a-' + key + '"', age_hours)


"""
3. Daily tier (design §2.3): stale out-of-window entries revalidate in
   partial cycles; fresh ones are skipped; in-window events are unaffected
"""


def test_daily_tier_revalidates_stale_out_of_window_event(monkeypatch):
    key = str(CURR_YEAR) + "stale"
    event = _event_obj(key, -30, -27, EventStatus.COMPLETED)
    _seed_event_manifest(key, age_hours=25)
    # DB etag matches the returned etag: without the daily tier this event
    # would be skipped entirely (out of window, nothing changed).
    etag = ETag(CURR_YEAR, key + "/matches", 'W/"m-' + key + '"')
    fakes = RecordingTBA(monkeypatch, {key: _completed_matches(key)})

    _, objs = data_tba.process_year(
        CURR_YEAR, [], _objs(event, [etag]), partial=True, cache=False
    )

    # All three paths issued manifest-backed conditional GETs (etag=None,
    # cache=False -> get_tba sends the stored etag).
    assert fakes.named("matches:" + key) == [("matches:" + key, None, False)]
    assert fakes.named("rankings:" + key) == [("rankings:" + key, None, False)]
    assert fakes.named("alliances:" + key) == [("alliances:" + key, None, False)]
    # The revalidated data flowed into the normal update path (never dropped).
    assert key + "_qm1" in objs[4]
    assert key + "_f1m1" in objs[4]
    assert objs[2][key].status == EventStatus.COMPLETED


def test_daily_tier_skips_fresh_out_of_window_event(monkeypatch):
    key = str(CURR_YEAR) + "fresh"
    event = _event_obj(key, -30, -27, EventStatus.COMPLETED)
    _seed_event_manifest(key, age_hours=1)
    fakes = RecordingTBA(monkeypatch)

    data_tba.process_year(CURR_YEAR, [], _objs(event), partial=True, cache=False)

    # Year list only, on the manifest-backed path (etag=None) so a 304 hands
    # back the cached list instead of an empty one.
    assert fakes.calls == [("events", None, False)]


def test_daily_tier_ignores_events_without_manifest_state(monkeypatch):
    """A cold manifest must add zero requests: only paths that already hold
    etag state are revalidation candidates."""
    key = str(CURR_YEAR) + "cold"
    event = _event_obj(key, -30, -27, EventStatus.COMPLETED)
    fakes = RecordingTBA(monkeypatch)

    data_tba.process_year(CURR_YEAR, [], _objs(event), partial=True, cache=False)

    assert fakes.calls == [("events", None, False)]


def test_partial_cycle_takes_manifest_path_for_the_event_list(monkeypatch):
    """The year's event list must never ride the explicit-etag path: a 304
    there returns a bool, get_events bails with an empty list, and no event is
    re-evaluated. The offseason quality filters run inside get_events, so an
    event dropped for "<6 teams" before its roster went up could otherwise
    never enter, however many cycles ran (2026mirr, July 2026)."""
    key = str(CURR_YEAR) + "list"
    event = _event_obj(key, -30, -27, EventStatus.COMPLETED)
    etag = ETag(CURR_YEAR, str(CURR_YEAR) + "/events", 'W/"stored"')
    fakes = RecordingTBA(monkeypatch)

    data_tba.process_year(
        CURR_YEAR, [], _objs(event, [etag]), partial=True, cache=False
    )

    assert fakes.named("events") == [("events", None, False)]


def test_daily_tier_leaves_in_window_events_on_explicit_etag_path(monkeypatch):
    key = str(CURR_YEAR) + "live"
    event = _event_obj(key, -1, 1, EventStatus.ONGOING)
    _seed_event_manifest(key, age_hours=25)  # stale, but in-window wins
    etag = ETag(CURR_YEAR, key + "/matches", 'W/"db"')
    fakes = RecordingTBA(monkeypatch, {key: []})

    data_tba.process_year(
        CURR_YEAR, [], _objs(event, [etag]), partial=True, cache=False
    )

    # In-window partial events keep today's explicit-etag conditional GET.
    assert fakes.named("matches:" + key) == [("matches:" + key, 'W/"db"', False)]


"""
4. Trust-cache for full current-year rebuilds (design §2.3, §8): completed
   events validated <24h ago serve the pickle (cache=True -> zero requests);
   stale or active-window paths revalidate (cache=False -> conditional GET)
"""


def test_reset_trusts_fresh_completed_event(monkeypatch):
    key = str(CURR_YEAR) + "done"
    event = _event_obj(key, -30, -27, EventStatus.COMPLETED)
    _seed_event_manifest(key, age_hours=1)
    fakes = RecordingTBA(monkeypatch, {key: _completed_matches(key)})

    data_tba.process_year(CURR_YEAR, [], _objs(event), partial=False, cache=False)

    # cache=True: get_tba serves the pickle without any TBA request.
    assert fakes.named("matches:" + key) == [("matches:" + key, None, True)]
    assert fakes.named("rankings:" + key) == [("rankings:" + key, None, True)]
    assert fakes.named("alliances:" + key) == [("alliances:" + key, None, True)]


def test_reset_revalidates_stale_completed_event(monkeypatch):
    key = str(CURR_YEAR) + "old"
    event = _event_obj(key, -30, -27, EventStatus.COMPLETED)
    _seed_event_manifest(key, age_hours=25)
    fakes = RecordingTBA(monkeypatch, {key: _completed_matches(key)})

    data_tba.process_year(CURR_YEAR, [], _objs(event), partial=False, cache=False)

    # cache=False: one conditional GET per path (never more than today).
    assert fakes.named("matches:" + key) == [("matches:" + key, None, False)]
    assert fakes.named("rankings:" + key) == [("rankings:" + key, None, False)]
    assert fakes.named("alliances:" + key) == [("alliances:" + key, None, False)]


def test_reset_never_trusts_active_window_event(monkeypatch):
    key = str(CURR_YEAR) + "now"
    event = _event_obj(key, -1, 1, EventStatus.ONGOING)
    _seed_event_manifest(key, age_hours=0)  # fresh, but active tier wins
    fakes = RecordingTBA(monkeypatch, {key: _completed_matches(key)})

    data_tba.process_year(CURR_YEAR, [], _objs(event), partial=False, cache=False)

    assert fakes.named("matches:" + key) == [("matches:" + key, None, False)]
    assert fakes.named("rankings:" + key) == [("rankings:" + key, None, False)]


def test_reset_historical_year_keeps_plain_cache(monkeypatch):
    """Past years pass cache=True today; the tier logic must not disturb it
    (no manifest state needed)."""
    year = CURR_YEAR - 1
    key = str(year) + "hist"
    event = _event_obj(key, -400, -397, EventStatus.COMPLETED, year=year)
    fakes = RecordingTBA(monkeypatch, {key: _completed_matches(key)})

    data_tba.process_year(year, [], _objs(event), partial=False, cache=True)

    assert fakes.named("matches:" + key) == [("matches:" + key, None, True)]


"""
5. REFRESH_TBA force flag: unconditional, etag-less fetches for the run
"""


def test_force_refresh_bypasses_cache_and_etag(tmp_path, monkeypatch):
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path))
    url = "event/2026iri/matches"
    path = os.path.join(str(tmp_path), url)
    os.makedirs(path)
    with open(os.path.join(path, "data.p"), "wb") as f:
        pickle.dump(["cached"], f)
    tba_cache._manifest[url] = {
        "etag": 'W/"old"',
        "last_validated": "2026-07-20T00:00:00Z",
    }
    sent = {}

    def fake(u, etag=None):
        sent["etag"] = etag
        return ["fresh"], 'W/"new"'

    monkeypatch.setattr(tba_main, "_get_tba", fake)
    tba_cache.set_force_refresh(True)

    data, etag = tba_main.get_tba(url, etag=None, cache=True)

    assert data == ["fresh"] and etag == 'W/"new"'
    assert sent["etag"] is None  # unconditional: no If-None-Match
    assert tba_cache.stored_etag(url) == 'W/"new"'  # archive state rebuilt
    with open(os.path.join(path, "data.p"), "rb") as f:
        assert pickle.load(f) == ["fresh"]


def test_force_refresh_env_var(monkeypatch):
    monkeypatch.delenv("REFRESH_TBA", raising=False)
    assert tba_cache.force_refresh() is False
    monkeypatch.setenv("REFRESH_TBA", "1")
    assert tba_cache.force_refresh() is True


def test_reset_state_clears_force_flag():
    tba_cache.set_force_refresh(True)
    tba_cache.reset_state()
    assert tba_cache.force_refresh() is False


def test_update_curr_year_scopes_force_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        data_main,
        "_update_curr_year",
        lambda partial, tba_partial: seen.setdefault(
            "forced", tba_cache.force_refresh()
        ),
    )

    data_main.update_curr_year(partial=False, tba_partial=False, refresh_tba=True)

    assert seen["forced"] is True
    assert tba_cache.force_refresh() is False  # cleared after the run


def test_reset_all_years_scopes_force_flag(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        data_main,
        "_reset_all_years",
        lambda: seen.setdefault("forced", tba_cache.force_refresh()),
    )

    data_main.reset_all_years(refresh_tba=True)

    assert seen["forced"] is True
    assert tba_cache.force_refresh() is False
