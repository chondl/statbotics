import attr

from src.db.models import Event, TeamEvent, TeamYear


def test_team_year_rank_only_drift_is_detected():
    ty1 = TeamYear(team=254, year=2026, count=10, epa=100.0, total_epa_rank=1)
    ty2 = attr.evolve(ty1, total_epa_rank=2)
    assert str(ty1) == str(ty2)
    assert ty1 != ty2


def test_team_year_norm_epa_drift_is_detected():
    ty1 = TeamYear(team=254, year=2026, count=10, epa=100.0, norm_epa=1500.0)
    ty2 = attr.evolve(ty1, norm_epa=1510.0)
    assert str(ty1) == str(ty2)
    assert ty1 != ty2


def test_identical_team_years_are_equal():
    ty1 = TeamYear(team=254, year=2026, count=10, epa=100.0)
    ty2 = attr.evolve(ty1)
    assert ty1 == ty2


def test_event_equality_sees_all_fields():
    e1 = Event(key="2026test", year=2026, name="Test", num_teams=30)
    e2 = attr.evolve(e1, num_teams=31)
    assert e1 != e2


def test_team_event_component_epa_drift_is_detected():
    te1 = TeamEvent(team=254, year=2026, event="2026test", epa=50.0, auto_epa=10.0)
    te2 = attr.evolve(te1, auto_epa=11.0)
    assert str(te1) == str(te2)
    assert te1 != te2


def test_comparison_with_none_counts_as_changed():
    ty = TeamYear(team=254, year=2026)
    assert ty != None  # noqa: E711
