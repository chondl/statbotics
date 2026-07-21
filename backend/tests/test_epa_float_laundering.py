import numpy as np

from src.db.models import TeamEvent, TeamYear
from src.models.epa.main import EPA
from src.models.epa.math import EPARating

EPA_FIELDS = [
    "epa",
    "auto_epa",
    "teleop_epa",
    "endgame_epa",
    "rp_1_epa",
    "rp_2_epa",
    "rp_3_epa",
    "tiebreaker_epa",
] + [f"comp_{i}_epa" for i in range(10)]


def make_model(team=254, year=2026):
    # post_record_team only reads self.year_num and self.epas[team].mean.
    model = EPA()
    model.year_num = year
    model.epas = {team: EPARating(np.linspace(0.123, 90.456, 18))}
    return model


def test_post_record_team_leaves_pure_python_floats():
    # np.round returns np.float64 (a float subclass stdlib json tolerates but
    # orjson/pickle/parquet reject or mangle); the assignments must launder
    # through float(). type() is deliberate — isinstance would pass np.float64.
    te = TeamEvent(team=254, year=2026, event="2026cc")
    ty = TeamYear(team=254, year=2026)
    make_model().post_record_team(254, te, ty)

    for field in EPA_FIELDS:
        for obj in (te, ty):
            value = getattr(obj, field)
            assert value is not None
            assert (
                type(value) is float
            ), f"{type(obj).__name__}.{field} is {type(value).__name__}"


def test_post_record_team_result_pure_python_floats():
    result = make_model().post_record_team(254, None, None)
    assert set(EPA_FIELDS) <= set(result)
    for field in EPA_FIELDS:
        assert type(result[field]) is float, f"result[{field!r}]"
