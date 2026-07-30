from src.data.epa.agg import compact_from_match, process_year_epas
from src.db.models import Match
from src.types.enums import CompLevel, MatchStatus


def mk_match(key, event, week, time, team, post_epa):
    m = Match(
        key=key,
        year=2026,
        event=event,
        week=week,
        elim=False,
        comp_level=CompLevel.QUAL,
        set_number=1,
        match_number=1,
        time=time,
        status=MatchStatus.COMPLETED,
        red_1=team,
        red_2=2,
        red_3=3,
        blue_1=4,
        blue_2=5,
        blue_3=6,
        red_dq="",
        red_surrogate="",
        blue_dq="",
        blue_surrogate="",
    )
    m.pre_epas = {str(team): {"epa": post_epa}}
    m.epas = {str(team): {"epa": post_epa}}
    return m


def test_offseason_matches_excluded_from_season_epa():
    team = 254
    regular = mk_match("2026casj_qm1", "2026casj", 3, 100, team, 80.0)
    offseason = mk_match("2026cc_qm1", "2026cc", 9, 200, team, 150.0)

    _, end, max_ = process_year_epas([regular, offseason], team, 0.0, {"2026cc"})

    assert end == 80.0, "season EPA must ignore the offseason match"
    assert max_ == 80.0


def test_offseason_matches_included_when_not_flagged():
    """Guard against the filter silently matching nothing."""
    team = 254
    regular = mk_match("2026casj_qm1", "2026casj", 3, 100, team, 80.0)
    offseason = mk_match("2026cc_qm1", "2026cc", 9, 200, team, 150.0)

    _, end, _ = process_year_epas([regular, offseason], team, 0.0, set())
    assert end == 150.0


def test_compact_from_match_flags_offseason():
    team = 254
    regular = mk_match("2026casj_qm1", "2026casj", 3, 100, team, 80.0)
    offseason = mk_match("2026cc_qm1", "2026cc", 9, 200, team, 150.0)

    assert compact_from_match(regular, team, {"2026cc"})["offseason"] is False
    assert compact_from_match(offseason, team, {"2026cc"})["offseason"] is True
