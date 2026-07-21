from src.db.models import Event, Match, TeamEvent, TeamYear, Year
from src.models.template import Model
from src.models.types import AlliancePred, Attribution
from src.types.enums import CompLevel, EventType, MatchStatus


class RecordingModel(Model):
    def __init__(self):
        super().__init__()
        self.updated = []

    def predict_match(self, match, event):
        return 0.5, AlliancePred(10.0, None), AlliancePred(10.0, None)

    def attribute_match(self, match, red_pred, blue_pred):
        return {t: Attribution() for t in match.get_red() + match.get_blue()}

    def update_team(self, team, attrib, match):
        self.updated.append(team)


def mk_match(event_key, week):
    return Match(
        key=f"{event_key}_qm1",
        year=2026,
        event=event_key,
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
        red_score=20,
        blue_score=10,
        red_no_foul=20,
        blue_no_foul=10,
    )


def run_model(event_type, week):
    model = RecordingModel()
    model.start_season(Year(year=2026), {}, {})
    event = Event(key="2026x", year=2026, name="X", type=event_type, week=week)
    match = mk_match("2026x", week)
    teams = [1, 2, 3, 4, 5, 6]
    team_events = {t: TeamEvent(team=t, year=2026, event="2026x") for t in teams}
    team_years = {t: TeamYear(team=t, year=2026) for t in teams}
    model.process_match(match, event, team_events, team_years)
    return model, match


def test_offseason_match_skips_epa_update():
    model, match = run_model(EventType.OFFSEASON, 9)
    assert model.updated == []
    # predictions and post-match records are still produced
    assert match.pre_epas is not None
    assert match.epas is not None


def test_regular_match_updates_epa():
    model, _ = run_model(EventType.REGIONAL, 1)
    assert sorted(model.updated) == [1, 2, 3, 4, 5, 6]
