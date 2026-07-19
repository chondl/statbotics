from src.constants import DISABLE_DB

if DISABLE_DB:
    from src.db_duckdb import (
        get_events,
        get_team_events,
        get_team_years,
        get_teams,
    )
else:
    from src.db.read import (
        get_events,
        get_team_events,
        get_team_years,
        get_teams,
    )

__all__ = [
    "get_events",
    "get_team_events",
    "get_team_years",
    "get_teams",
]
