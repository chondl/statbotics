"""Offseason stale-probe regression tests (2026wvrox incident, 2026-07-31).

TBA ETags are not reliable content fingerprints: 2026wvrox's roster went
0 -> 30 teams without its `teams/simple` etag changing, so every conditional
revalidation 304'd and re-served an empty cached roster. The `<6 teams`
quality filter then dropped the event on every cycle, forever.

Two fixes under test:
1. Quality-filter probes never send If-None-Match — they either serve a
   tier-fresh pickle or refetch unconditionally. Events inside their date
   window probe fresh on every cycle.
2. check_year_partial also watches in-window type-99 events that are NOT yet
   ingested, probing them fresh and escalating when they would now pass the
   quality filters (nothing else triggers a recompute for a dropped event).
"""

import os
from datetime import datetime, timedelta
from types import SimpleNamespace

import src.data.tba as data_tba
import src.tba.cache as tba_cache
import src.tba.main as tba_main
import src.tba.read_tba as rt
from src.tba.utils import dump_cache, load_cache

YEAR = 2026
TODAY = datetime.now().strftime("%Y-%m-%d")
TOMORROW = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
FUTURE = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")
PAST_2D = (datetime.now() - timedelta(days=2)).strftime("%Y-%m-%d")


def mk_event(key, start_date, end_date):
    return {
        "key": key,
        "event_type": 99,
        "district": None,
        "week": None,
        "name": f"Event {key}",
        "country": "USA",
        "state_prov": "WV",
        "start_date": start_date,
        "end_date": end_date,
        "webcasts": [],
    }


def mk_teams(n):
    return [{"key": f"frc{i}"} for i in range(1, n + 1)]


def mk_match():
    return {
        "alliances": {
            "red": {"team_keys": ["frc1", "frc2", "frc3"]},
            "blue": {"team_keys": ["frc4", "frc5", "frc6"]},
        }
    }


class LyingTbaSession:
    """Serves current content on unconditional GETs, but 304s ANY conditional
    GET whose If-None-Match matches the stored etag — even when the content
    has changed since that etag was recorded. This is the observed 2026wvrox
    TBA behavior."""

    def __init__(self, payloads):
        self.payloads = payloads  # path -> (json, etag)
        self.calls = []  # (path, if_none_match or None)

    def get(self, url, headers=None, **kwargs):
        path = url.replace(tba_main.read_prefix, "")
        inm = (headers or {}).get("If-None-Match")
        self.calls.append((path, inm))
        json_data, etag = self.payloads[path]
        if inm is not None and inm == etag:
            return SimpleNamespace(status_code=304, headers={})
        return SimpleNamespace(
            status_code=200, json=lambda: json_data, headers={"ETag": etag}
        )


def _seed_pickle(cache_dir, url, data):
    dump_cache(os.path.join(cache_dir, url), data)


def _seed_manifest(url, etag, age_hours):
    ts = datetime.utcnow() - timedelta(hours=age_hours)
    tba_cache._manifest[url] = {
        "etag": etag,
        "last_validated": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _rig(monkeypatch, tmp_path, payloads):
    session = LyingTbaSession(payloads)
    monkeypatch.setattr(tba_main, "session", session)
    monkeypatch.setattr(tba_main, "TBA_CACHE_DIR", str(tmp_path))
    return session


"""
1. get_tba fresh=True: unconditional refetch that rewrites the pickle
"""


def test_get_tba_fresh_ignores_pickle_and_etag(monkeypatch, tmp_path):
    url = "event/2026wvx/teams/simple"
    _seed_pickle(str(tmp_path), url, [])
    _seed_manifest(url, 'W/"same"', age_hours=1)
    session = _rig(monkeypatch, tmp_path, {url: (mk_teams(30), 'W/"same"')})

    data, etag = tba_main.get_tba(url, etag=None, cache=False, fresh=True)

    assert len(data) == 30
    assert session.calls == [(url, None)]  # no If-None-Match sent
    assert len(load_cache(os.path.join(str(tmp_path), url))) == 30  # rewritten


"""
2. get_events probes: in-window events bypass a tier-fresh stale pickle
   (the 2026wvrox repro: empty roster pickle, lying 304s, live event)
"""


def _wvrox_payloads(key, start, end, n_teams, matches):
    return {
        f"events/{YEAR}": ([mk_event(key, start, end)], 'W/"list"'),
        f"event/{key}/teams/simple": (mk_teams(n_teams), 'W/"same"'),
        f"event/{key}/matches": (matches, 'W/"m"'),
    }


def test_in_window_event_probes_fresh_despite_tier(monkeypatch, tmp_path):
    key = "2026wvx"
    _rig(monkeypatch, tmp_path, _wvrox_payloads(key, TODAY, TOMORROW, 30, [mk_match()]))
    # Stale empty roster pickle, manifest validated 1h ago: the tier alone
    # would serve the pickle and drop the event.
    _seed_pickle(str(tmp_path), f"event/{key}/teams/simple", [])
    _seed_pickle(str(tmp_path), f"event/{key}/matches", [])
    _seed_manifest(f"event/{key}/teams/simple", 'W/"same"', age_hours=1)
    _seed_manifest(f"event/{key}/matches", 'W/"m"', age_hours=1)

    events, _ = rt.get_events(YEAR, etag=None, cache=False, tier_probes=True)

    assert key in {e["key"] for e in events}


def test_out_of_window_stale_revalidation_is_unconditional(monkeypatch, tmp_path):
    # The 2026vagle2 repro: event months away, roster filled after the empty
    # pickle was cached, TBA 304s against the stored etag. When the 24h tier
    # expires, the revalidation must be a full GET, not a conditional one.
    key = "2026vgl"
    session = _rig(monkeypatch, tmp_path, _wvrox_payloads(key, FUTURE, FUTURE, 19, []))
    _seed_pickle(str(tmp_path), f"event/{key}/teams/simple", [])
    _seed_pickle(str(tmp_path), f"event/{key}/matches", [])
    _seed_manifest(f"event/{key}/teams/simple", 'W/"same"', age_hours=25)
    _seed_manifest(f"event/{key}/matches", 'W/"m"', age_hours=25)

    events, _ = rt.get_events(YEAR, etag=None, cache=False, tier_probes=True)

    assert key in {e["key"] for e in events}
    probe_calls = [c for c in session.calls if key in c[0]]
    assert all(inm is None for _path, inm in probe_calls)  # never conditional


def test_out_of_window_tier_fresh_probe_serves_pickle(monkeypatch, tmp_path):
    # Steady state stays cheap: out-of-window + validated <24h = no network.
    key = "2026calm"
    session = _rig(monkeypatch, tmp_path, _wvrox_payloads(key, FUTURE, FUTURE, 30, []))
    _seed_pickle(str(tmp_path), f"event/{key}/teams/simple", mk_teams(30))
    _seed_pickle(str(tmp_path), f"event/{key}/matches", [])
    _seed_manifest(f"event/{key}/teams/simple", 'W/"same"', age_hours=1)
    _seed_manifest(f"event/{key}/matches", 'W/"m"', age_hours=1)

    events, _ = rt.get_events(YEAR, etag=None, cache=False, tier_probes=True)

    assert key in {e["key"] for e in events}
    assert [c for c in session.calls if key in c[0]] == []  # pickle only


"""
3. check_year_partial: in-window dropped offseason events can trigger a cycle
"""


def _gate_rig(monkeypatch, candidates, probe_results, probe_calls):
    def fake_events(year, etag=None, cache=True, tier_probes=False):
        return [], etag  # events-list etag unchanged

    def fake_candidates(year):
        return candidates

    def fake_probe(key):
        probe_calls.append(key)
        return probe_results[key]

    monkeypatch.setattr(data_tba, "get_events_tba", fake_events)
    monkeypatch.setattr(data_tba, "get_raw_offseason_events_tba", fake_candidates)
    monkeypatch.setattr(data_tba, "probe_offseason_event_tba", fake_probe)


def test_gate_fires_for_viable_unindexed_inwindow_event(monkeypatch):
    calls = []
    _gate_rig(
        monkeypatch,
        [mk_event("2026wvx", TODAY, TOMORROW)],
        {"2026wvx": (list(range(1, 31)), 125)},
        calls,
    )
    assert data_tba.check_year_partial(YEAR, [], []) is True
    assert calls == ["2026wvx"]


def test_gate_quiet_while_unindexed_event_still_empty(monkeypatch):
    calls = []
    _gate_rig(
        monkeypatch,
        [mk_event("2026wvx", TODAY, TOMORROW)],
        {"2026wvx": ([1, 2, 3], 0)},
        calls,
    )
    assert data_tba.check_year_partial(YEAR, [], []) is False
    assert calls == ["2026wvx"]


def test_gate_skips_out_of_window_candidates(monkeypatch):
    calls = []
    _gate_rig(
        monkeypatch,
        [mk_event("2026far", FUTURE, FUTURE)],
        {"2026far": (list(range(1, 31)), 0)},
        calls,
    )
    assert data_tba.check_year_partial(YEAR, [], []) is False
    assert calls == []


def test_gate_skips_already_ingested_events(monkeypatch):
    from src.types.enums import EventStatus

    calls = []
    _gate_rig(
        monkeypatch,
        [mk_event("2026wvx", TODAY, TOMORROW)],
        {"2026wvx": (list(range(1, 31)), 125)},
        calls,
    )
    ingested = SimpleNamespace(
        key="2026wvx",
        status=EventStatus.COMPLETED,
        start_date=TODAY,
        end_date=TOMORROW,
        qual_matches=0,
        current_match=0,
        time=0,
    )
    assert data_tba.check_year_partial(YEAR, [ingested], []) is False
    assert calls == []


def test_gate_quiet_for_matchless_event_past_end(monkeypatch):
    # 30 teams but 0 matches and ended >=1 day ago: get_events would still
    # drop it (matchless past), so escalating would loop a recompute per hour
    # for nothing.
    calls = []
    _gate_rig(
        monkeypatch,
        [mk_event("2026ghost", PAST_2D, PAST_2D)],
        {"2026ghost": (list(range(1, 31)), 0)},
        calls,
    )
    assert data_tba.check_year_partial(YEAR, [], []) is False
    assert calls == ["2026ghost"]
