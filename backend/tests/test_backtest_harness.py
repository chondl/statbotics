from src.db.models import Match
from src.models.backtest import Metrics, bucket_of, score_metrics_for
from src.types.enums import CompLevel, MatchStatus, MatchWinner


def test_bucket_of():
    assert bucket_of(0) == "1-20"
    assert bucket_of(19) == "1-20"
    assert bucket_of(20) == "21-50"
    assert bucket_of(49) == "21-50"
    assert bucket_of(50) == "51+"


def mk_match(red_pred, blue_pred, red_actual, blue_actual, win_prob):
    m = Match(
        key="2026cc_qm1",
        year=2026,
        event="2026cc",
        week=9,
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
        red_score=red_actual,
        blue_score=blue_actual,
        red_no_foul=red_actual,
        blue_no_foul=blue_actual,
    )
    m.epa_red_score_pred = red_pred
    m.epa_blue_score_pred = blue_pred
    m.epa_win_prob = win_prob
    m.epa_winner = MatchWinner.RED if win_prob >= 0.5 else MatchWinner.BLUE
    return m


def test_score_metrics_for_perfect_prediction():
    m = mk_match(100.0, 50.0, 100, 50, 1.0)
    out = score_metrics_for([m])
    assert isinstance(out, Metrics)
    assert out.count == 1
    assert out.rmse == 0.0
    assert out.bias == 0.0
    assert out.acc == 1.0
    assert out.brier == 0.0


def test_score_metrics_for_biased_prediction():
    # predicts 10 high on both alliances; red still correctly favored
    m = mk_match(110.0, 60.0, 100, 50, 0.9)
    out = score_metrics_for([m])
    assert out.bias == -10.0  # actual - predicted, so negative means "ran high"
    assert out.rmse == 10.0
    assert out.acc == 1.0
    assert abs(out.brier - 0.01) < 1e-9


def test_score_metrics_for_empty():
    out = score_metrics_for([])
    assert out.count == 0
