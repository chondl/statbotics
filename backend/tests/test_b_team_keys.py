"""B/C/D-team support: bit-packed team ids and their display form.

Second robots (frc604B) have always been dropped by Statbotics — the
offseason quality filter treated an unparseable team key as grounds to
discard the whole event, which cost 6 of the 64 type-99 events in 2026,
including Sunset Showdown (24 of its 52 matches). They are packed into the
high bits of the integer team id every table already stores.
"""

import src.tba.read_tba as rt
from src.tba.clean_data import (
    TEAM_NUMBER_MASK,
    format_team,
    is_synthetic_team,
    parse_team,
)
from src.types.enums import EventType
from tests.test_offseason_ingest import YEAR, mk_event, mk_teams, patch_tba


def test_plain_team_keys_are_unchanged():
    assert parse_team("frc604") == 604
    assert parse_team("604") == 604
    assert parse_team("frc11258") == 11258


def test_suffixed_team_keys_pack_into_high_bits():
    assert parse_team("frc604B") == (1 << 16) | 604
    assert parse_team("frc604C") == (2 << 16) | 604
    assert parse_team("frc604D") == (3 << 16) | 604
    # 2026azrl1 fields frc498E, so the range runs past D.
    assert parse_team("frc498E") == (4 << 16) | 498
    assert parse_team("frc498Z") == (25 << 16) | 498


def test_packed_ids_never_collide_with_real_team_numbers():
    # Every real FRC number fits the low 16 bits, so no packed id can alias
    # one. ~12000 issued today against a 65535 ceiling.
    assert parse_team("frc604B") > TEAM_NUMBER_MASK
    assert parse_team("frc65535") == TEAM_NUMBER_MASK


def test_display_round_trips():
    for key in ["604", "604B", "254C", "1323D", "498E", "11258"]:
        assert format_team(parse_team(key)) == key


def test_unparseable_team_still_raises():
    # Callers rely on the raise to drop genuinely broken data.
    for bad in ["frcXYZ", "frc604BB", "", "frc70000"]:
        try:
            parse_team(bad)
            raise AssertionError(f"expected ValueError for {bad!r}")
        except ValueError:
            pass


def test_b_team_event_is_ingested(monkeypatch):
    # Was test_b_team_event_dropped: the event is now kept, with the B team
    # carried as a distinct competitor rather than deleting the event.
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
    out, _ = rt.get_events(YEAR)
    events = {e["key"]: e for e in out}
    assert "2026bt" in events
    assert events["2026bt"]["type"] == EventType.OFFSEASON


def test_b_team_reaches_match_rows(monkeypatch):
    payload = [_tba_match("2026bt_qm1", ["frc604B", "frc2", "frc3"], [4, 5, 6])]
    monkeypatch.setattr(
        rt, "get_tba", lambda path, etag=None, cache=True: (payload, None)
    )

    matches, _ = rt.get_event_matches(YEAR, "2026bt", 0)

    assert matches[0]["red_1"] == parse_team("frc604B")
    assert matches[0]["red_2"] == 2


def _tba_match(key, red_keys, blue_nums):
    return {
        "key": key,
        "comp_level": "qm",
        "set_number": 1,
        "match_number": 1,
        "alliances": {
            "red": {
                "team_keys": red_keys,
                "dq_team_keys": [],
                "surrogate_team_keys": [],
                "score": 10,
            },
            "blue": {
                "team_keys": [f"frc{t}" for t in blue_nums],
                "dq_team_keys": [],
                "surrogate_team_keys": [],
                "score": 5,
            },
        },
        "winning_alliance": "red",
        "score_breakdown": None,
        "videos": [],
        "time": 1,
        "predicted_time": None,
        "actual_time": 1,
    }


def test_rankings_survive_a_b_team_row(monkeypatch):
    """get_event_rankings parses inside a bare `except: pass`, so one
    unreadable row used to return the partially-built dict and every team
    after it rendered as rank -1. 2026sunshow ranked frc5507B 10th of 31,
    which cost the other 22 teams their rank."""
    ranks = [
        {"team_key": "frc581", "rank": 1},
        {"team_key": "frc973", "rank": 2},
        {"team_key": "frc5507B", "rank": 3},
        {"team_key": "frc604", "rank": 4},
        {"team_key": "frc841", "rank": 5},
    ]
    monkeypatch.setattr(
        rt, "get_tba", lambda path, etag=None, cache=True: ({"rankings": ranks}, None)
    )

    out, _ = rt.get_event_rankings("2026sunshow")

    # Every team keeps its rank, including the ones after the B team.
    assert out[581] == 1
    assert out[973] == 2
    assert out[604] == 4
    assert out[841] == 5
    assert out[parse_team("frc5507B")] == 3


"""
Synthetic competitors must stay out of year-level populations. norm_epa is a
rank/percentile mapping over the year's teams and the *_epa_rank fields index
sorted team lists, so letting these into the population shifted norm_epa for
1604 real teams on the first deploy. Upstream never had them: its offseason
filters dropped any event containing one.
"""


def test_placeholder_demo_robots_are_synthetic():
    for team in [9970, 9985, 9999]:
        assert is_synthetic_team(team)


def test_packed_second_robots_are_synthetic():
    for key in ["frc604B", "frc498E", "frc10988B"]:
        assert is_synthetic_team(parse_team(key))


def test_real_teams_are_not_synthetic():
    # 9969 sits just under the placeholder band; 11258 is a real high number.
    for team in [1, 254, 604, 9969, 10000, 11258, TEAM_NUMBER_MASK]:
        assert not is_synthetic_team(team)


def test_synthetic_teams_excluded_from_year_population():
    """The exclusion must be at the population level, not by dropping the
    team-year: their event pages still need epa and team_matches."""
    import src.data.epa.agg as agg

    src = open(agg.__file__).read()
    assert "if is_synthetic_team(ty.team):" in src
    # The guard sits after the per-team fields are computed.
    assert src.index("ty.team_matches = ") < src.index("if is_synthetic_team(ty.team):")
