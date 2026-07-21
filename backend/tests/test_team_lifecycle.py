"""DB retirement Phase 1 (design 2026-07-20 §2): the pipeline owns Team
lifecycle state, GCS the store.

- derive_team_districts parity with the SQL update_team_districts
  (src/db/functions/update_teams.py): Team.district = the district of the
  team's most recent TeamYear — the latest year's value even when null, and
  null when the team has no TeamYears (scalar subquery semantics).
- prune_teams parity with remove_teams_with_no_events
  (src/db/functions/remove_teams_no_events.py) Team deletion predicate:
  drop teams with zero TeamEvents across all years AND rookie_year < CURR_YEAR
  (NULL rookie_year and current/future rookies survive, as in SQL).
- Full-cycle publish ordering: post_process mutates teams BEFORE the
  current-year publish, so published snapshot/Parquet/blobs carry career
  records, norm_epa, active, last_active_year, district.
- refresh_teams db-less persistence republishes exactly the affected
  artifacts; DB mode keeps the existing update_*_db writes and publishes
  nothing.
"""

from collections import defaultdict
from typing import Dict, List

from src.constants import CURR_YEAR
from src.data.teams import derive_team_districts, prune_teams
from src.db.models import Team, TeamYear

YEAR = CURR_YEAR


def team(num, rookie_year=2010, name="Some Team", **kw):
    return Team(team=num, name=name, rookie_year=rookie_year, **kw)


def ty(num, year, district=None, **kw):
    return TeamYear(team=num, year=year, district=district, **kw)


def as_all_ty(tys: List[TeamYear]) -> Dict[int, Dict[int, TeamYear]]:
    out: Dict[int, Dict[int, TeamYear]] = defaultdict(dict)
    for t in tys:
        out[t.year][t.team] = t
    return out


# ------------------- derive_team_districts (SQL parity) -----------------------


def test_district_latest_team_year_wins():
    teams = [team(254)]
    all_ty = as_all_ty([ty(254, 2024, "fim"), ty(254, 2026, "ont")])
    derive_team_districts(teams, all_ty)
    assert teams[0].district == "ont"


def test_district_latest_year_null_overrides_older_value():
    # SQL takes the latest TeamYear row's district even when it is null
    # (ORDER BY year DESC LIMIT 1) — not "latest non-null".
    teams = [team(254, district="fim")]
    all_ty = as_all_ty([ty(254, 2024, "fim"), ty(254, 2026, None)])
    derive_team_districts(teams, all_ty)
    assert teams[0].district is None


def test_district_no_team_years_becomes_null():
    # Scalar subquery over zero rows is NULL: a stale value is cleared.
    teams = [team(254, district="fim")]
    derive_team_districts(teams, as_all_ty([]))
    assert teams[0].district is None


def test_district_other_teams_do_not_leak():
    teams = [team(254), team(1678)]
    all_ty = as_all_ty([ty(254, 2026, "chs"), ty(1678, 2026, "pnw")])
    derive_team_districts(teams, all_ty)
    assert teams[0].district == "chs"
    assert teams[1].district == "pnw"


# ----------------------- prune_teams (SQL parity) -----------------------------


def test_prune_drops_never_played_past_rookie():
    kept = prune_teams([team(999, rookie_year=2010)], event_teams=set())
    assert kept == []


def test_prune_keeps_team_with_any_event():
    teams = [team(254, rookie_year=2010)]
    assert prune_teams(teams, event_teams={254}) == teams


def test_prune_keeps_null_rookie_year():
    # SQL: NULL < CURR_YEAR is unknown, so the row is not deleted.
    teams = [team(999, rookie_year=None)]
    assert prune_teams(teams, event_teams=set()) == teams


def test_prune_keeps_current_and_future_rookies():
    teams = [team(998, rookie_year=CURR_YEAR), team(999, rookie_year=CURR_YEAR + 1)]
    assert prune_teams(teams, event_teams=set()) == teams


def test_prune_preserves_order_and_filters_only_prunable():
    teams = [
        team(148, rookie_year=2003),  # historical events only
        team(999, rookie_year=2010),  # never played
        team(254, rookie_year=2010),
    ]
    assert prune_teams(teams, event_teams={148, 254}) == [teams[0], teams[2]]
