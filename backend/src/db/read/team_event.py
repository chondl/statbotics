from typing import List, Optional, Set

from sqlalchemy.orm.session import Session as SessionType

from src.db.main import Session
from src.db.models.team_event import TeamEvent, TeamEventORM
from src.db.read.main import common_filters
from src.db.transaction import run_transaction


def get_team_event(team: int, event: str) -> Optional[TeamEvent]:
    def callback(session: SessionType):
        data = session.query(TeamEventORM).filter(
            TeamEventORM.team == team, TeamEventORM.event == event
        )
        out_data: Optional[TeamEventORM] = data.first()
        if out_data is None:
            return None
        return TeamEvent.from_dict(out_data.__dict__)

    return run_transaction(Session, callback)  # type: ignore


def get_team_event_teams() -> Set[int]:
    """Distinct team numbers with at least one TeamEvent across all years —
    the same set remove_teams_with_no_events queries (DB retirement Phase 1:
    feeds the publish-time no-event prune)."""

    def callback(session: SessionType):
        return {
            x[0]
            for x in session.query(TeamEventORM.team).group_by(TeamEventORM.team).all()
        }

    return run_transaction(Session, callback)  # type: ignore


def get_team_events(
    team: Optional[int] = None,
    teams: Optional[List[str]] = None,
    year: Optional[int] = None,
    event: Optional[str] = None,
    country: Optional[str] = None,
    state: Optional[str] = None,
    district: Optional[str] = None,
    type: Optional[str] = None,
    week: Optional[int] = None,
    metric: Optional[str] = None,
    ascending: Optional[bool] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[TeamEvent]:
    @common_filters(TeamEventORM, TeamEvent, metric, ascending, limit, offset)
    def callback(session: SessionType):
        data = session.query(TeamEventORM)
        if team is not None:
            data = data.filter(TeamEventORM.team == team)
        if teams is not None:
            data = data.filter(TeamEventORM.team.in_(teams))
        if year is not None:
            data = data.filter(TeamEventORM.year == year)
        if event is not None:
            data = data.filter(TeamEventORM.event == event)
        if country is not None:
            data = data.filter(TeamEventORM.country == country)
        if state is not None:
            data = data.filter(TeamEventORM.state == state)
        if district is not None:
            data = data.filter(TeamEventORM.district == district)
        if type is not None:
            data = data.filter(TeamEventORM.type == type)
        if week is not None:
            data = data.filter(TeamEventORM.week == week)

        return data

    return run_transaction(Session, callback)  # type: ignore
