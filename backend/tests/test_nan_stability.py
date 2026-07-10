import attr

from src.data.utils import nan_safe_eq
from src.db.models import Match, TeamEvent, TeamYear


def nan():
    # a fresh NaN each call: mirrors a recomputed value vs one read back from the
    # DB/snapshot, which are distinct objects (identity short-circuit does not hide
    # the NaN != NaN inequality)
    return float("nan")


def test_identical_nan_field_is_equal():
    ty1 = TeamYear(team=254, year=2026, epa=nan())
    ty2 = attr.evolve(ty1, epa=nan())
    assert ty1 != ty2  # attrs field-wise !=: distinct NaN objects compare unequal
    assert nan_safe_eq(ty1, ty2)  # nan_safe_eq: NaN == NaN


def test_real_drift_still_detected_with_nan_present():
    ty1 = TeamYear(team=254, year=2026, epa=nan(), norm_epa=1500.0)
    ty2 = attr.evolve(ty1, epa=nan(), norm_epa=1510.0)
    assert not nan_safe_eq(ty1, ty2)


def test_non_nan_change_still_detected():
    te1 = TeamEvent(team=254, year=2026, event="2026test", epa=50.0, auto_epa=10.0)
    te2 = attr.evolve(te1, auto_epa=11.0)
    assert not nan_safe_eq(te1, te2)


def test_none_counts_as_changed():
    ty = TeamYear(team=254, year=2026)
    assert not nan_safe_eq(ty, None)


def test_db_gate_skips_identical_nan_rows():
    # mirrors the changed() call site in src/data/utils.py
    curr = {ty.pk(): ty for ty in [TeamYear(team=254, year=2026, epa=nan())]}
    prev = {pk: attr.evolve(ty, epa=nan()) for pk, ty in curr.items()}
    old_gate = [o for o in curr.values() if o != prev.get(o.pk())]
    new_gate = [o for o in curr.values() if not nan_safe_eq(o, prev.get(o.pk()))]
    assert old_gate != []  # old attrs-!= gate churns the NaN row
    assert new_gate == []  # nan_safe_eq gate does not


def test_storage_gate_ordered_list_with_nan_is_stable():
    # mirrors event_to_matches[e.key] != orig_matches[e.key] in storage.py
    m1 = Match(key="2026test_qm1", year=2026, event="2026test", epa_red_score_pred=nan())
    m2 = Match(key="2026test_qm2", year=2026, event="2026test")
    curr = [m1, m2]
    orig = [attr.evolve(m1, epa_red_score_pred=nan()), attr.evolve(m2)]
    assert curr != orig  # list eq is elementwise ==, broken by distinct NaNs
    assert nan_safe_eq(curr, orig)


def test_storage_gate_detects_real_match_change():
    m1 = Match(key="2026test_qm1", year=2026, event="2026test", red_score=10)
    curr = [m1]
    orig = [attr.evolve(m1, red_score=17)]
    assert not nan_safe_eq(curr, orig)
