"""Pre-kickoff 2027 season: the year must start without any matches."""

from src.breakdown import all_keys
from src.data.avg import process_year as process_year_avg
from src.data.utils import create_objs
from src.models.epa.init import get_init_epa
from src.tba.clean_data import clean_district


def test_2027_has_standard_keys():
    keys = all_keys[2027]
    for k in ("rp_1", "rp_2", "rp_3", "tiebreaker_points"):
        assert k in keys
    assert len(keys) == 18


def test_2027_starts_with_no_matches():
    year = process_year_avg(create_objs(2027)[0], [])
    assert year.score_sd and year.score_sd > 0
    rookie = get_init_epa(year, None, None, 0.4)
    assert rookie.mean[0] > 0


def test_unknown_district_is_none():
    assert clean_district("zzz") is None
    assert clean_district("ca") == "ca"
    assert clean_district("tx") == "fit"
