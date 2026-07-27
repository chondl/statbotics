from datetime import datetime, timedelta

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


def patch_tba(monkeypatch, events, extra=None):
    payloads = {f"events/{YEAR}": events, "events/2024": events, **(extra or {})}

    def _get(path, etag=None, cache=True):
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


def test_placeholder_team_event_dropped(monkeypatch):
    patch_tba(
        monkeypatch,
        [mk_event("2026ph", 99)],
        {
            "event/2026ph/teams/simple": mk_teams([1, 2, 3, 4, 5, 9971]),
            "event/2026ph/matches": [mk_match([1, 2, 3], [4, 5, 9971])],
        },
    )
    assert "2026ph" not in get_keys()


def test_b_team_event_dropped(monkeypatch):
    patch_tba(
        monkeypatch,
        [mk_event("2026bt", 99)],
        {
            "event/2026bt/teams/simple": mk_teams(range(1, 40)),
            "event/2026bt/matches": [
                {
                    "alliances": {
                        "red": {"team_keys": ["frc254B", "frc2", "frc3"]},
                        "blue": {"team_keys": ["frc4", "frc5", "frc6"]},
                    }
                }
            ],
        },
    )
    assert "2026bt" not in get_keys()


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


def test_regular_regional_unaffected(monkeypatch):
    patch_tba(monkeypatch, [mk_event("2026gal", 0, week=0)])
    events = get_keys()
    assert events["2026gal"]["type"] == EventType.REGIONAL
    assert events["2026gal"]["week"] == 1
