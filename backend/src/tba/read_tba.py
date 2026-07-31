import time
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple, cast

from src.tba import cache as tba_cache
from src.tba.breakdown import clean_breakdown, post_clean_breakdown
from src.tba.clean_data import clean_district, clean_state, get_match_time, parse_team
from src.tba.constants import (
    DISTRICT_OVERRIDES,
    EVENT_BLACKLIST,
    EVENT_TYPE_OVERRIDES,
    MATCH_BLACKLIST,
)
from src.tba.main import get_tba
from src.tba.types import EventDict, MatchDict, TeamDict
from src.types.enums import CompLevel, EventType, MatchStatus, MatchWinner


def get_timestamp_from_str(date: str):
    return int(time.mktime(datetime.strptime(date, "%Y-%m-%d").timetuple()))


BREAKDOWN_GRACE_SECONDS = 24 * 3600


def defer_missing_breakdown(
    year: int, completed: bool, has_breakdown: bool, match_time: int, now_ts: int
) -> bool:
    return (
        year >= 2016
        and completed
        and not has_breakdown
        and now_ts - match_time < BREAKDOWN_GRACE_SECONDS
    )


def get_teams(cache: bool = True) -> List[TeamDict]:
    out: List[TeamDict] = []
    for i in range(50):
        data, _ = get_tba("teams/" + str(i), etag=None, cache=cache)
        if type(data) is bool:
            continue
        for data_team in data:
            num = data_team["key"][3:]
            new_data: TeamDict = {
                "team": num,
                "name": data_team["nickname"],
                "rookie_year": data_team["rookie_year"],
                "country": data_team["country"],
                "state": clean_state(data_team["state_prov"]),
            }
            out.append(new_data)
    return out


def get_districts(
    year: int, etag: Optional[str] = None, cache: bool = True
) -> Tuple[List[Tuple[str, str]], Optional[str]]:
    out: List[Tuple[str, str]] = []
    data, new_etag = get_tba("districts/" + str(year), etag=etag, cache=cache)
    if type(data) is bool:
        return out, new_etag
    for district in data:
        out.append((district["key"], district["abbreviation"]))
    return out, new_etag


def get_district_teams(
    district: str, etag: Optional[str] = None, cache: bool = True
) -> Tuple[List[int], Optional[str]]:
    out: List[int] = []
    data, new_etag = get_tba(
        "district/" + str(district) + "/teams", etag=etag, cache=cache
    )
    if type(data) is bool:
        return out, new_etag
    for team in data:
        out.append(int(team["key"][3:]))
    return out, new_etag


def get_event_teams(
    event: str,
    etag: Optional[str] = None,
    cache: bool = True,
    fresh: bool = False,
) -> Tuple[List[int], Optional[str]]:
    query_str = "event/" + str(event) + "/teams/simple"
    data, new_etag = get_tba(query_str, etag=etag, cache=cache, fresh=fresh)
    if type(data) is bool:
        return [], new_etag
    out = [parse_team(x["key"]) for x in data]
    return out, new_etag


OFFSEASON_REVALIDATION_HOURS = 24

# Probe-freshness window: start-1d .. end+1d. Inside it, rosters and
# schedules change hour to hour (day-of imports), so probes always refetch.
PROBE_WINDOW_BEFORE_DAYS = 1
PROBE_WINDOW_AFTER_DAYS = 1


def _in_probe_window(start_date: str, end_date: str) -> bool:
    try:
        start = datetime.strptime(start_date, "%Y-%m-%d")
        end = datetime.strptime(end_date, "%Y-%m-%d")
    except ValueError:
        return False
    now = datetime.now()
    return (
        start - timedelta(days=PROBE_WINDOW_BEFORE_DAYS)
        <= now
        <= end + timedelta(days=PROBE_WINDOW_AFTER_DAYS + 1)
    )


def _probe_mode(
    url: str, cache: bool, tier_probes: bool, in_window: bool
) -> Tuple[bool, bool]:
    """(cache, fresh) for one offseason quality-filter probe.

    Freshness tier (TBA cache design §2.3): partial cycles re-run the filters
    every cycle so that an event which failed them earlier (empty roster, no
    schedule yet) can enter once TBA fills in — but that must not mean two
    network round-trips per type-99 event per cycle, so the pickle is served
    unless the manifest says this path has gone stale. A full cycle is an
    explicit "rebuild from TBA": it must refetch every probe, or a roster
    cached while the event was still empty survives the rebuild and the event
    stays dropped (2026mirr, first deploy of the filters).

    Any probe that does go to the network goes UNCONDITIONALLY — never with
    If-None-Match. TBA's etags are not content fingerprints: 2026wvrox's
    roster went 0 -> 30 teams with no etag change, so a conditional
    revalidation 304'd and re-served the empty pickle on every cycle, keeping
    the event dropped forever. Events inside their date window skip the tier
    entirely and refetch every cycle (day-of roster/schedule imports)."""
    if in_window:
        return False, True
    if cache:
        return True, False
    if tier_probes and not tba_cache.needs_revalidation(
        url, OFFSEASON_REVALIDATION_HOURS
    ):
        return True, False
    return False, True


def get_raw_offseason_events(year: int) -> List[Dict[str, Any]]:
    """Raw TBA event dicts for the type-99 events subject to the offseason
    quality filters, served from the cached events-list pickle (no network
    when the pickle exists). Used by check_year_partial to watch events that
    were dropped by the filters — nothing else re-evaluates them, because a
    roster or schedule filling in moves neither the year's events-list etag
    nor any etag the gate otherwise checks."""
    if year < 2025:
        return []
    data, _ = get_tba("events/" + str(year), etag=None, cache=True)
    if type(data) is bool:
        return []
    out: List[Dict[str, Any]] = []
    for event in data:
        key = event["key"]
        if "tempclone" in key or key in EVENT_BLACKLIST:
            continue
        if int(event["event_type"]) != 99 or key in EVENT_TYPE_OVERRIDES:
            continue
        out.append(event)
    return out


def probe_offseason_event(key: str) -> Tuple[List[int], int]:
    """Fresh (unconditional — see _probe_mode on why never conditional)
    quality-filter probe for one offseason event: (parsed team ids, raw
    match count). Broken data reads as not viable."""
    try:
        teams = get_event_teams(key, etag=None, cache=False, fresh=True)[0]
        matches = get_tba(
            "event/" + key + "/matches", etag=None, cache=False, fresh=True
        )[0]
        if type(matches) is bool:
            return teams, 0
        return teams, len(matches)
    except Exception:
        return [], 0


def get_events(
    year: int,
    etag: Optional[str] = None,
    cache: bool = True,
    tier_probes: bool = False,
) -> Tuple[List[EventDict], Optional[str]]:
    out: List[EventDict] = []
    data, new_etag = get_tba("events/" + str(year), etag=etag, cache=cache)
    if type(data) is bool:
        return out, new_etag

    for event in data:
        key: str = event["key"]

        if "tempclone" in key:
            continue

        # filters out partial/missing events
        if key in EVENT_BLACKLIST:
            continue

        event_type_int = int(event["event_type"])
        if event_type_int in (99, 100) and key not in EVENT_TYPE_OVERRIDES:
            if event_type_int == 100:
                continue  # preseason
            # offseason events are ingested for 2025+ with quality filters
            if year < 2025:
                continue
            try:
                teams_path = f"event/{key}/teams/simple"
                matches_path = f"event/{key}/matches"
                in_window = _in_probe_window(event["start_date"], event["end_date"])
                teams_cache, teams_fresh = _probe_mode(
                    teams_path, cache, tier_probes, in_window
                )
                event_teams = get_event_teams(
                    key, etag=None, cache=teams_cache, fresh=teams_fresh
                )[0]
                # remove events with less than 6 teams
                if len(event_teams) < 6:
                    continue
                # Placeholder teams (FIRST's 9970-9999 demo entries) are NOT
                # grounds to drop the event: they routinely pad rosters at
                # otherwise healthy offseason events. The EPA protection sits
                # a layer down — src/models/template.py sets skip_update for
                # any match containing one, and offseason events are frozen
                # regardless. Stripping them here is not an option either:
                # get_event_matches drops any alliance with <3 teams, so
                # removing a demo team would delete the real match with it.
                matches_cache, matches_fresh = _probe_mode(
                    matches_path, cache, tier_probes, in_window
                )
                matches = get_tba(
                    matches_path,
                    etag=None,
                    cache=matches_cache,
                    fresh=matches_fresh,
                )[0]
                end_date = datetime.strptime(event["end_date"], "%Y-%m-%d")
                if len(matches) == 0 and (datetime.now() - end_date).days >= 1:  # type: ignore
                    continue
                for match in matches:  # type: ignore
                    all_teams = match["alliances"]["red"]["team_keys"]
                    all_teams += match["alliances"]["blue"]["team_keys"]
                    # B/C/D teams parse into packed ids rather than raising, so
                    # they no longer take the event down with them. Anything
                    # still unparseable is genuinely broken data: drop it.
                    all_teams = [parse_team(x) for x in all_teams]
            except Exception:
                continue

        event_type_dict: Dict[int, EventType] = defaultdict(lambda: EventType.INVALID)
        event_type_dict[0] = EventType.REGIONAL
        event_type_dict[1] = EventType.DISTRICT
        event_type_dict[2] = EventType.DISTRICT_CMP
        event_type_dict[3] = EventType.CHAMPS_DIV
        event_type_dict[4] = EventType.EINSTEIN
        # rename district divisions to district championship
        event_type_dict[5] = EventType.DISTRICT_CMP
        # rename festival of championships to einsteins
        event_type_dict[6] = EventType.EINSTEIN
        event_type_dict[99] = EventType.OFFSEASON

        event_type = event_type_dict[event_type_int]
        if key in EVENT_TYPE_OVERRIDES:
            event_type = EVENT_TYPE_OVERRIDES[key]

        if event["district"] is not None:
            event["district"] = event["district"]["abbreviation"]

        if event["key"] in DISTRICT_OVERRIDES:
            event["district"] = DISTRICT_OVERRIDES[event["key"]]

        # assigns worlds to week 8
        if event_type.is_champs():
            event["week"] = 8

        if event_type == EventType.OFFSEASON:
            event["week"] = 9

        # filter out incomplete events
        if "week" not in event or event["week"] is None:
            continue

        # bug in TBA API
        if year != 2016 and event_type in [
            EventType.REGIONAL,
            EventType.DISTRICT,
            EventType.DISTRICT_CMP,
        ]:
            event["week"] += 1

        video: Optional[str] = None
        webcasts = event["webcasts"]
        if len(webcasts) > 0:
            video_type = webcasts[0]["type"]
            if video_type == "twitch":
                video = "https://www.twitch.tv/" + webcasts[0]["channel"]
            elif video_type == "youtube":
                video = "https://www.youtube.com/watch?v=" + webcasts[0]["channel"]

            if video is not None and len(video) > 50:
                video = None

        new_data: EventDict = {
            "year": year,
            "key": key,
            "name": cast(str, event["name"])[:100],
            "country": cast(str, event["country"]),
            "state": clean_state(event["state_prov"]),
            "district": clean_district(event["district"]),
            "start_date": cast(str, event["start_date"]),
            "end_date": cast(str, event["end_date"]),
            "time": get_timestamp_from_str(event["start_date"]),
            "type": event_type,
            "week": cast(int, event["week"]),
            "video": video,
        }

        out.append(new_data)

    return out, new_etag


def get_event_matches(
    year: int,
    event: str,
    event_time: int,
    etag: Optional[str] = None,
    cache: bool = True,
) -> Tuple[List[MatchDict], Optional[str]]:
    out: List[MatchDict] = []
    query_str = "event/" + str(event) + "/matches"
    matches, new_etag = get_tba(query_str, etag=etag, cache=cache)

    if type(matches) is bool:
        return out, new_etag

    now_ts = int(datetime.now().timestamp())

    for match in matches:
        red_teams: List[str] = match["alliances"]["red"]["team_keys"]
        red_dq_teams: List[str] = match["alliances"]["red"]["dq_team_keys"]
        red_surrogate_teams: List[str] = match["alliances"]["red"][
            "surrogate_team_keys"
        ]
        blue_teams: List[str] = match["alliances"]["blue"]["team_keys"]
        blue_dq_teams: List[str] = match["alliances"]["blue"]["dq_team_keys"]
        blue_surrogate_teams: List[str] = match["alliances"]["blue"][
            "surrogate_team_keys"
        ]

        if match["key"] in MATCH_BLACKLIST:
            continue

        if year <= 2004 and (len(set(red_teams)) < 2 or len(set(blue_teams)) < 2):
            continue

        if year > 2004 and (len(set(red_teams)) < 3 or len(set(blue_teams)) < 3):
            continue

        if len(set(red_teams).intersection(set(blue_teams))) > 0:
            continue

        red_score = match.get("alliances", {}).get("red", {}).get("score", None)
        blue_score = match.get("alliances", {}).get("blue", {}).get("score", None)
        status = MatchStatus.UPCOMING
        if red_score >= 0 and blue_score >= 0:
            status = MatchStatus.COMPLETED

        winner = None
        if status == MatchStatus.COMPLETED:
            raw_winner: Optional[str] = match.get("winning_alliance", None)
            if raw_winner == "red":
                winner = MatchWinner.RED
            elif raw_winner == "blue":
                winner = MatchWinner.BLUE
            elif red_score > blue_score:
                winner = MatchWinner.RED
            elif blue_score > red_score:
                winner = MatchWinner.BLUE
            else:
                winner = MatchWinner.TIE

            if year == 2015 and match["comp_level"] == "qm":
                winner = None

        red_teams = [str(parse_team(team)) for team in red_teams]
        blue_teams = [str(parse_team(team)) for team in blue_teams]

        breakdown = match.get("score_breakdown", {}) or {}
        red_breakdown = clean_breakdown(
            match["key"], "red", year, breakdown.get("red", None), red_score
        )
        blue_breakdown = clean_breakdown(
            match["key"], "blue", year, breakdown.get("blue", None), blue_score
        )

        red_breakdown, blue_breakdown = post_clean_breakdown(
            match["key"], year, red_breakdown, blue_breakdown
        )

        video = None
        if "videos" in match:
            if len(match["videos"]) > 0:
                if match["videos"][0]["type"] == "youtube":
                    video = match["videos"][0]["key"].split("&")[0].split("?")[0]
                    if len(video) > 20:
                        video = None

        time: int = match["time"] or get_match_time(
            match["comp_level"],
            match["set_number"],
            match["match_number"],
            event_time,
        )

        if defer_missing_breakdown(
            year,
            status == MatchStatus.COMPLETED,
            breakdown.get("red") is not None and breakdown.get("blue") is not None,
            time,
            now_ts,
        ):
            status = MatchStatus.UPCOMING
            winner = None
            red_score = None
            blue_score = None

        comp_level = CompLevel.INVALID
        if match["comp_level"] == "qm":
            comp_level = CompLevel.QUAL
        elif match["comp_level"] == "ef":
            comp_level = CompLevel.EIGHTH
        elif match["comp_level"] == "qf":
            comp_level = CompLevel.QUARTER
        elif match["comp_level"] == "sf":
            comp_level = CompLevel.SEMI
        elif match["comp_level"] == "f":
            comp_level = CompLevel.FINAL

        match_data: MatchDict = {
            "event": event,
            "key": cast(str, match["key"]),
            "comp_level": comp_level,
            "set_number": cast(int, match["set_number"]),
            "match_number": cast(int, match["match_number"]),
            "status": status,
            "video": video,
            "red_1": int(red_teams[0]),
            "red_2": int(red_teams[1]),
            "red_3": int(red_teams[2]) if len(red_teams) > 2 else None,
            "red_dq": ",".join([str(parse_team(t)) for t in red_dq_teams]),
            "red_surrogate": ",".join(
                [str(parse_team(t)) for t in red_surrogate_teams]
            ),
            "blue_1": int(blue_teams[0]),
            "blue_2": int(blue_teams[1]),
            "blue_3": int(blue_teams[2]) if len(blue_teams) > 2 else None,
            "blue_dq": ",".join([str(parse_team(t)) for t in blue_dq_teams]),
            "blue_surrogate": ",".join(
                [str(parse_team(t)) for t in blue_surrogate_teams]
            ),
            "winner": winner,
            "time": time,
            "predicted_time": cast(Optional[int], match["predicted_time"]),
            "red_score": red_score,
            "blue_score": blue_score,
            "red_score_breakdown": red_breakdown,
            "blue_score_breakdown": blue_breakdown,
        }
        out.append(match_data)

    return out, new_etag


def get_event_rankings(
    event: str, etag: Optional[str] = None, cache: bool = True
) -> Tuple[Dict[int, int], Optional[str]]:
    out: Dict[int, int] = {}
    new_etag: Optional[str] = None
    # queries TBA for rankings, some older events are not populated
    try:
        query_str = "event/" + str(event) + "/rankings"
        data, new_etag = get_tba(query_str, etag=etag, cache=cache)
        if type(data) is bool:
            return out, new_etag
        rankings = data["rankings"]
        for ranking in rankings:
            # Per-entry: the outer except returns whatever was parsed so far,
            # so one unreadable row used to silently truncate the rest of the
            # table. Every team after the first B team lost its rank and
            # rendered as -1 (2026sunshow: frc5507B ranked 10th of 31, so 22
            # teams showed -1).
            try:
                out[parse_team(ranking["team_key"])] = ranking["rank"]
            except (ValueError, KeyError, TypeError):
                continue
    except Exception:
        pass

    return out, new_etag


def get_event_alliances(
    event: str, etag: Optional[str] = None, cache: bool = True
) -> Tuple[Tuple[Dict[int, str], Dict[int, bool]], Optional[str]]:
    alliance_dict: Dict[int, str] = {}
    captain_dict: Dict[int, bool] = {}
    new_etag: Optional[str] = None
    # queries TBA for alliances, some older events are not populated
    try:
        query_str = "event/" + str(event) + "/alliances"
        data, new_etag = get_tba(query_str, etag=etag, cache=cache)
        if type(data) is bool:
            return (alliance_dict, captain_dict), new_etag
        for alliance in data:
            captain = alliance["picks"][0]
            for team in alliance["picks"]:
                team_num = team[3:]
                alliance_dict[team_num] = alliance["name"]
                captain_dict[team_num] = team == captain
    except Exception:
        pass

    return (alliance_dict, captain_dict), new_etag
