from datetime import datetime, timedelta

import src.tba.cache as tba_cache
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


def patch_tba(monkeypatch, events, extra=None, calls=None):
    payloads = {f"events/{YEAR}": events, "events/2024": events, **(extra or {})}

    def _get(path, etag=None, cache=True, fresh=False):
        if calls is not None:
            calls.append((path, etag, cache, fresh))
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


def test_placeholder_team_event_kept(monkeypatch):
    """FIRST's demo teams (9970-9999) fill out odd rosters at offseason
    events. Dropping the whole event over them cost ~27 of 64 type-99 events
    in 2026 alone. The EPA protection they were guarding lives one layer
    down: template.py sets skip_update for any match containing a placeholder
    team, and offseason events are frozen regardless."""
    patch_tba(
        monkeypatch,
        [mk_event("2026ph", 99)],
        {
            "event/2026ph/teams/simple": mk_teams([1, 2, 3, 4, 5, 9971]),
            "event/2026ph/matches": [mk_match([1, 2, 3], [4, 5, 9971])],
        },
    )
    events = get_keys()
    assert "2026ph" in events
    assert events["2026ph"]["type"] == EventType.OFFSEASON
    assert events["2026ph"]["week"] == 9


# B-team events are no longer dropped — they are ingested with the second
# robot packed into the team id. See tests/test_b_team_keys.py.


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


def test_district_forced_offseason_by_override(monkeypatch):
    # 2026isde1, 2026isde2, and 2026iscmp are real entries in
    # EVENT_TYPE_OVERRIDES (-> OFFSEASON): official Israeli district events
    # that ran AFTER Worlds 2026 finished (war-delayed), so they must not
    # move season EPA/standings or next season's seed. The override makes
    # them week-9 offseason events; the sandbox handles the rest downstream.
    patch_tba(
        monkeypatch,
        [
            mk_event("2026isde1", 1, week=16),
            mk_event("2026isde2", 1, week=17),
            mk_event("2026iscmp", 2, week=18),
        ],
    )
    events = get_keys()
    for key in ("2026isde1", "2026isde2", "2026iscmp"):
        assert events[key]["type"] == EventType.OFFSEASON
        assert events[key]["week"] == 9


def test_pre_worlds_israeli_districts_stay_district(monkeypatch):
    # 2026isde3/2026isde4 ran BEFORE Worlds and are deliberately absent from
    # EVENT_TYPE_OVERRIDES: they stay ordinary district events and keep
    # updating season EPA.
    patch_tba(monkeypatch, [mk_event("2026isde3", 1, week=2)])
    events = get_keys()
    assert events["2026isde3"]["type"] == EventType.DISTRICT
    assert events["2026isde3"]["week"] == 3


def test_regular_regional_unaffected(monkeypatch):
    patch_tba(monkeypatch, [mk_event("2026gal", 0, week=0)])
    events = get_keys()
    assert events["2026gal"]["type"] == EventType.REGIONAL
    assert events["2026gal"]["week"] == 1


"""
Offseason quality-filter probes are tiered (TBA cache design §2.3). Partial
cycles re-run the filters every cycle, so the per-event roster/match fetches
must not hit the network every time: serve the pickle unless the manifest
says the path has gone stale.
"""


def _seed(url, age_hours):
    ts = datetime.utcnow() - timedelta(hours=age_hours)
    tba_cache._manifest[url] = {
        "etag": 'W/"x"',
        "last_validated": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _probe_flags(monkeypatch, key="2026tier", tier_probes=True):
    calls = []
    patch_tba(
        monkeypatch,
        [mk_event(key, 99)],
        {
            f"event/{key}/teams/simple": mk_teams(range(1, 40)),
            f"event/{key}/matches": [mk_match([1, 2, 3], [4, 5, 6])],
        },
        calls=calls,
    )
    rt.get_events(YEAR, cache=False, tier_probes=tier_probes)
    return {path: cache for path, _etag, cache, _fresh in calls if key in path}


def test_fresh_offseason_probes_serve_the_pickle(monkeypatch):
    key = "2026fresh"
    _seed(f"event/{key}/teams/simple", age_hours=1)
    _seed(f"event/{key}/matches", age_hours=1)
    flags = _probe_flags(monkeypatch, key)
    assert flags[f"event/{key}/teams/simple"] is True
    assert flags[f"event/{key}/matches"] is True


def test_stale_offseason_probes_revalidate(monkeypatch):
    key = "2026stale"
    _seed(f"event/{key}/teams/simple", age_hours=25)
    _seed(f"event/{key}/matches", age_hours=25)
    flags = _probe_flags(monkeypatch, key)
    assert flags[f"event/{key}/teams/simple"] is False
    assert flags[f"event/{key}/matches"] is False


def test_cold_offseason_probes_fetch_normally(monkeypatch):
    # No manifest state: nothing to revalidate against, so the plain cache
    # path runs and get_tba fetches because no pickle exists yet.
    key = "2026cold"
    tba_cache._manifest.pop(f"event/{key}/teams/simple", None)
    tba_cache._manifest.pop(f"event/{key}/matches", None)
    flags = _probe_flags(monkeypatch, key)
    assert flags[f"event/{key}/teams/simple"] is True
    assert flags[f"event/{key}/matches"] is True


def test_full_cycle_probes_keep_plain_cache(monkeypatch):
    # cache=True (historical/full runs) is untouched by the tier.
    key = "2026full"
    _seed(f"event/{key}/teams/simple", age_hours=99)
    calls = []
    patch_tba(
        monkeypatch,
        [mk_event(key, 99)],
        {
            f"event/{key}/teams/simple": mk_teams(range(1, 40)),
            f"event/{key}/matches": [mk_match([1, 2, 3], [4, 5, 6])],
        },
        calls=calls,
    )
    rt.get_events(YEAR, cache=True)
    flags = {p: c for p, _e, c, _f in calls if key in p}
    assert flags[f"event/{key}/teams/simple"] is True


def test_full_cycle_revalidates_every_probe(monkeypatch):
    """A full cycle is an explicit rebuild from TBA, so the tier must not
    apply: a roster cached while the event was still empty would otherwise
    survive the rebuild and keep the event dropped. 2026mirr hit exactly this
    on the first deploy — TBA had 30 teams, the reprocess served a stale
    pickle, and the event stayed missing."""
    key = "2026rebuild"
    _seed(f"event/{key}/teams/simple", age_hours=1)  # fresh: the tier would skip
    _seed(f"event/{key}/matches", age_hours=1)
    flags = _probe_flags(monkeypatch, key, tier_probes=False)
    assert flags[f"event/{key}/teams/simple"] is False
    assert flags[f"event/{key}/matches"] is False
