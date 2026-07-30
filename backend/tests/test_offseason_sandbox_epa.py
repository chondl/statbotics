from collections import defaultdict

import numpy as np

from src.db.models import Event, Match, TeamEvent, TeamYear, Year
from src.models.epa.main import EPA
from src.models.epa.math import EPARating
from src.models.template import Model
from src.models.types import AlliancePred, Attribution
from src.types.enums import CompLevel, EventType, MatchStatus


class RecordingModel(Model):
    def __init__(self):
        super().__init__()
        self.updated = []
        self.recorded_tys = []

    def predict_match(self, match, event):
        return 0.5, AlliancePred(10.0, None), AlliancePred(10.0, None)

    def attribute_match(self, match, red_pred, blue_pred):
        return {t: Attribution() for t in match.get_red() + match.get_blue()}

    def update_team(self, team, attrib, match):
        self.updated.append(team)

    def post_record_team(self, team, te, ty):
        self.recorded_tys.append(ty)
        return {}


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


def test_offseason_match_updates_rating():
    """Offseason matches now update. The base Model carries no per-team state,
    so the fork is a no-op for it and update_team still fires."""
    model, match = run_model(EventType.OFFSEASON, 9)
    assert sorted(model.updated) == [1, 2, 3, 4, 5, 6]
    assert match.pre_epas is not None
    assert match.epas is not None


def test_offseason_match_does_not_stamp_team_year():
    model, _ = run_model(EventType.OFFSEASON, 9)
    assert model.recorded_tys == [None] * 6


def test_regular_match_updates_epa_and_stamps_team_year():
    model, _ = run_model(EventType.REGIONAL, 1)
    assert sorted(model.updated) == [1, 2, 3, 4, 5, 6]
    assert all(ty is not None for ty in model.recorded_tys)


def mk_epa_model():
    """EPA with rating state installed directly. start_season needs a fully
    populated Year (get_init_epa reads mean components), which these tests do
    not exercise."""
    model = EPA()
    model.year_num = 2026
    model.num_teams = 3
    model.epas = defaultdict(lambda: EPARating(np.zeros(18)))
    model.counts = defaultdict(int)
    model.epas[254] = EPARating(np.array([100.0] + [0.0] * 17))
    model.counts[254] = 40
    return model


def test_sandbox_forks_on_first_touch_and_leaves_real_state_alone():
    model = mk_epa_model()
    model.enter_sandbox("2026cc")
    model.epas[254].mean[0] = 999.0
    model.counts[254] += 1
    model.exit_sandbox()

    assert model.epas[254].mean[0] == 100.0
    assert model.counts[254] == 40


def test_sandbox_seeds_count_from_constant():
    from src.models.epa.constants import SANDBOX_SEED_COUNT

    model = mk_epa_model()
    model.enter_sandbox("2026cc")
    assert model.counts[254] == SANDBOX_SEED_COUNT
    model.exit_sandbox()


def test_two_offseason_events_are_independent():
    model = mk_epa_model()

    model.enter_sandbox("2026iri")
    model.epas[254].mean[0] = 150.0
    model.exit_sandbox()

    model.enter_sandbox("2026cc")
    assert model.epas[254].mean[0] == 100.0  # seeded from real, not from IRI
    model.exit_sandbox()

    model.enter_sandbox("2026iri")
    assert model.epas[254].mean[0] == 150.0  # IRI kept its own evolution
    model.exit_sandbox()


def test_sandbox_restored_after_exception():
    model = mk_epa_model()
    real_epas, real_counts = model.epas, model.counts

    event = Event(key="2026x", year=2026, name="X", type=EventType.OFFSEASON, week=9)
    try:
        with model._sandbox(event):
            raise RuntimeError("boom")
    except RuntimeError:
        pass

    assert model.epas is real_epas
    assert model.counts is real_counts


def test_unknown_team_seeds_from_init_rating():
    model = mk_epa_model()
    model.enter_sandbox("2026cc")
    assert model.epas[9999].mean[0] == 0.0  # the defaultdict's init rating
    model.exit_sandbox()


def test_zero_qual_count_fallback_records_sandbox_not_frozen():
    """wins.py excludes offseason matches from records, so qual_count is 0 for
    every offseason team event -- including ones that played a full schedule.
    calc.py's 'no matches played yet' fallback must therefore run inside the
    event's sandbox, or it pins TeamEvent.epa back to the frozen season rating.
    """
    model = mk_epa_model()
    event = Event(
        key="2026iri", year=2026, name="IRI", type=EventType.OFFSEASON, week=9
    )

    # the event's matches moved the fork well above the season rating
    model.enter_sandbox(event.key)
    model.epas[254].mean[0] = 281.7
    model.exit_sandbox()

    te = TeamEvent(team=254, year=2026, event="2026iri")
    with model._sandbox(event):
        model.post_record_team(254, te, None)

    assert te.epa == 281.7, "fallback must record the evolved fork"
    assert model.epas[254].mean[0] == 100.0, "real rating still untouched"
