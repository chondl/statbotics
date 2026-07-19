from src.constants import DISABLE_DB

if DISABLE_DB:
    from src.db_duckdb import get_noteworthy_matches, get_upcoming_matches
else:
    from src.db.functions import get_noteworthy_matches, get_upcoming_matches

__all__ = [
    "get_noteworthy_matches",
    "get_upcoming_matches",
]
