from src.data.wins import process_year
from src.db.models import Event, Match, TeamEvent, TeamYear, Year
from src.types.enums import CompLevel, EventType, MatchStatus, MatchWinner


def mk_objs(event_type, week):
    year = Year(year=2026)
    event = Event(
        key="2026x",
        year=2026,
        name="X",
        type=event_type,
        week=week,
        start_date="2026-07-16",
        end_date="2026-07-18",
    )
    match = Match(
        key="2026x_qm1",
        year=2026,
        event="2026x",
        week=week,
        elim=False,
        comp_level=CompLevel.QUAL,
        set_number=1,
        match_number=1,
        time=0,
        status=MatchStatus.COMPLETED,
        red_1=1,
        red_2=2,
        red_3=3,
        blue_1=4,
        blue_2=5,
        blue_3=6,
        red_dq="",
        red_surrogate="",
        blue_dq="",
        blue_surrogate="",
        winner=MatchWinner.RED,
        red_score=20,
        blue_score=10,
    )
    ty = TeamYear(team=1, year=2026)
    te = TeamEvent(team=1, year=2026, event="2026x")
    return (
        year,
        {"2026_1": ty},
        {"2026x": event},
        {"1_2026x": te},
        {"2026x_qm1": match},
        {},
    )


def test_offseason_match_excluded_from_records():
    objs = mk_objs(EventType.OFFSEASON, 9)
    process_year(objs)
    ty = objs[1]["2026_1"]
    te = objs[3]["1_2026x"]
    assert (ty.wins, ty.losses, ty.ties, ty.count) == (0, 0, 0, 0)
    assert te.count == 0


def test_regular_match_counted():
    objs = mk_objs(EventType.REGIONAL, 1)
    process_year(objs)
    ty = objs[1]["2026_1"]
    assert (ty.wins, ty.count) == (1, 1)
