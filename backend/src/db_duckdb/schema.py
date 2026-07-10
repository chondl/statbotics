import json
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Type

from sqlalchemy import JSON, Boolean, Float, Integer, inspect
from sqlalchemy.sql.sqltypes import Enum as SQLEnum

from src.db.models import Event, Match, Team, TeamEvent, TeamYear, Year
from src.db.models.event import EventORM
from src.db.models.main import Model, ModelORM
from src.db.models.match import MatchORM
from src.db.models.team import TeamORM
from src.db.models.team_event import TeamEventORM
from src.db.models.team_year import TeamYearORM
from src.db.models.year import YearORM

PARQUET_PREFIX = "parquet"

SPECS: Dict[str, Tuple[Type[Model], Type[ModelORM]]] = {
    "team_years": (TeamYear, TeamYearORM),
    "events": (Event, EventORM),
    "team_events": (TeamEvent, TeamEventORM),
    "matches": (Match, MatchORM),
    "teams": (Team, TeamORM),
    "year": (Year, YearORM),
}

Column = Tuple[str, str, Optional[Type[Enum]]]


def _kind(column: Any) -> Tuple[str, Optional[Type[Enum]]]:
    type_ = column.type
    if isinstance(type_, SQLEnum) and type_.enum_class is not None:
        return "enum", type_.enum_class
    if isinstance(type_, JSON):
        return "json", None
    if isinstance(type_, Boolean):
        return "bool", None
    if isinstance(type_, Integer):
        return "int", None
    if isinstance(type_, Float):
        return "float", None
    return "str", None


def columns(orm_type: Type[ModelORM]) -> List[Column]:
    out: List[Column] = []
    for column in inspect(orm_type).columns:
        kind, enum_cls = _kind(column)
        out.append((column.name, kind, enum_cls))
    return out


def to_row(obj: Model, cols: List[Column]) -> Tuple[Any, ...]:
    values: List[Any] = []
    for name, kind, _ in cols:
        value = getattr(obj, name, None)
        if value is None:
            values.append(None)
        elif kind == "enum":
            values.append(value.value if isinstance(value, Enum) else value)
        elif kind == "json":
            values.append(json.dumps(value))
        else:
            values.append(value)
    return tuple(values)


def from_row(model_cls: Type[Model], cols: List[Column], row: Dict[str, Any]) -> Model:
    data: Dict[str, Any] = {}
    for name, kind, enum_cls in cols:
        value = row.get(name)
        if value is not None:
            if kind == "json":
                value = json.loads(value)
            elif kind == "enum" and enum_cls is not None:
                value = enum_cls(value)
        data[name] = value
    return model_cls.from_dict(data)
