import os

if os.environ.get("API_BACKEND", "").lower() == "duckdb":
    from src.db_duckdb import (
        get_event,
        get_events,
        get_match,
        get_matches,
        get_team,
        get_team_event,
        get_team_events,
        get_team_year,
        get_team_years,
        get_teams,
        get_year,
        get_years,
    )
else:
    from src.db.read import (
        get_event,
        get_events,
        get_match,
        get_matches,
        get_team,
        get_team_event,
        get_team_events,
        get_team_year,
        get_team_years,
        get_teams,
        get_year,
        get_years,
    )

__all__ = [
    "get_event",
    "get_events",
    "get_match",
    "get_matches",
    "get_team_event",
    "get_team_events",
    "get_team_year",
    "get_team_years",
    "get_team",
    "get_teams",
    "get_year",
    "get_years",
]
