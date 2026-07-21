"""In-pipeline Team lifecycle passes (DB retirement Phase 1, design
2026-07-20 §2 items 2-3).

These replicate the DB-side post-process functions in src/db/functions so
published Team state no longer depends on the database. In DB mode the SQL
versions still run (Phase 4 deletes them); these passes must agree with them.
"""

from typing import Dict, List, Optional, Set, Tuple

from src.constants import CURR_YEAR
from src.db.models import Team, TeamYear


def derive_team_districts(
    teams: List[Team], all_team_years: Dict[int, Dict[int, TeamYear]]
) -> List[Team]:
    """Equivalent of update_team_districts (src/db/functions/update_teams.py).

    The SQL sets Team.district to a scalar subquery over the team's TeamYears
    ORDER BY year DESC LIMIT 1 — i.e. the most recent TeamYear's district,
    even when that value is null, and null when the team has no TeamYears at
    all. Replicated exactly: "latest non-null district" would NOT match.
    """
    latest: Dict[int, Tuple[int, Optional[str]]] = {}
    for team_years in all_team_years.values():
        for ty in team_years.values():
            prev = latest.get(ty.team)
            if prev is None or ty.year > prev[0]:
                latest[ty.team] = (ty.year, ty.district)

    for team in teams:
        entry = latest.get(team.team)
        team.district = None if entry is None else entry[1]

    return teams


def prune_teams(teams: List[Team], event_teams: Set[int]) -> List[Team]:
    """Publish-time equivalent of remove_teams_with_no_events' Team deletion
    (src/db/functions/remove_teams_no_events.py): drop teams with zero
    TeamEvents across all years whose rookie_year is a known past year.

    Matching the SQL predicate `team NOT IN (event teams) AND rookie_year <
    CURR_YEAR`: teams with rookie_year NULL (unknown comparison) or >=
    CURR_YEAR (current/future rookies who have not played yet) survive even
    with no events.
    """
    return [
        t
        for t in teams
        if t.team in event_teams or t.rookie_year is None or t.rookie_year >= CURR_YEAR
    ]
