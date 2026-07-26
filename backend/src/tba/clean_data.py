from typing import Optional

from src.tba.constants import (
    CANADA_MAPPING,
    DISTRICT_MAPPING,
    USA_MAPPING,
    VALID_DISTRICTS,
)
from src.types.enums import CompLevel


def clean_state(state: str) -> Optional[str]:
    if state in USA_MAPPING:
        return USA_MAPPING[state]
    if state in CANADA_MAPPING:
        return CANADA_MAPPING[state]
    if state in USA_MAPPING.values():
        return state
    if state in CANADA_MAPPING.values():
        return state
    return None


def clean_district(district: Optional[str]) -> Optional[str]:
    if district in DISTRICT_MAPPING:
        district = DISTRICT_MAPPING[district]
    if district is not None and district not in VALID_DISTRICTS:
        return None
    return district


def get_match_time(
    comp_level: CompLevel, set_number: int, match_number: int, event_time: int
) -> int:
    match_time = event_time  # start value
    if comp_level == CompLevel.QUAL:
        match_time += match_number
    elif comp_level == CompLevel.EIGHTH:
        match_time += 200 + 10 * set_number + match_number
    elif comp_level == CompLevel.QUARTER:
        match_time += 300 + 10 * set_number + match_number
    elif comp_level == CompLevel.SEMI:
        match_time += 400 + 10 * set_number + match_number
    elif comp_level == CompLevel.FINAL:
        match_time += 500 + match_number
    else:
        raise ValueError("Invalid comp_level: " + comp_level)
    return match_time


# B/C/D teams (frc604B) are second robots a team brings to offseason events.
# They are real competitors with no team number of their own, and every team
# key in the schema is an integer, so they are packed into the high bits of
# one: team numbers occupy the low 16 (max 65535, against ~12000 issued
# today) and the suffix indexes the bits above. 604B -> (1 << 16) | 604.
# Statbotics has never represented these before — B-team events were dropped
# outright — so no stored data uses this range.
TEAM_NUMBER_BITS = 16
TEAM_NUMBER_MASK = (1 << TEAM_NUMBER_BITS) - 1
# B through Z, not just B/C/D: 2026azrl1 already fields frc498E. The whole
# alphabet costs 5 bits, so the widest id stays ~1.7M — still a plain int32.
TEAM_SUFFIXES = "BCDEFGHIJKLMNOPQRSTUVWXYZ"


def parse_team(value: str) -> int:
    """TBA team key or number -> integer team id. 'frc604'/'604' -> 604,
    'frc604B' -> 66140. Raises ValueError on anything else, which keeps the
    callers' existing "unparseable team -> drop" behavior intact."""
    raw = value[3:] if value.startswith("frc") else value
    suffix = 0
    if raw and raw[-1].upper() in TEAM_SUFFIXES:
        suffix = TEAM_SUFFIXES.index(raw[-1].upper()) + 1
        raw = raw[:-1]
    number = int(raw)
    if number < 0 or number > TEAM_NUMBER_MASK:
        raise ValueError(f"team number out of range: {value}")
    return (suffix << TEAM_NUMBER_BITS) | number


def format_team(team: int) -> str:
    """Inverse of parse_team, for display. 66140 -> '604B'."""
    suffix_index = team >> TEAM_NUMBER_BITS
    if suffix_index == 0:
        return str(team)
    if suffix_index > len(TEAM_SUFFIXES):
        return str(team)
    return f"{team & TEAM_NUMBER_MASK}{TEAM_SUFFIXES[suffix_index - 1]}"
