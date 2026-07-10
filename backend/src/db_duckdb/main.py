import os
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple, Type

import duckdb

from src.db.models.main import Model, ModelORM
from src.db_duckdb.schema import PARQUET_PREFIX, SPECS, columns, from_row

SYNC_TTL = float(os.environ.get("DUCKDB_SYNC_TTL", "30"))

_con: Optional[duckdb.DuckDBPyConnection] = None
_cache_dir: Optional[str] = None
_generations: Dict[str, int] = {}
_last_sync: float = 0.0


def _connection() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        _con = duckdb.connect()
    return _con


def _sync() -> str:
    global _cache_dir, _last_sync
    if _cache_dir is None:
        _cache_dir = tempfile.mkdtemp(prefix="duckdb_parquet_")
    now = time.monotonic()
    if _last_sync and now - _last_sync < SYNC_TTL:
        return _cache_dir
    from src.google.storage import _bucket

    for blob in _bucket().list_blobs(prefix=f"{PARQUET_PREFIX}/"):
        if _generations.get(blob.name) == blob.generation:
            continue
        dest = os.path.join(_cache_dir, blob.name)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        blob.download_to_filename(dest)
        _generations[blob.name] = blob.generation
    _last_sync = now
    return _cache_dir


def _years() -> List[int]:
    base = os.path.join(_sync(), PARQUET_PREFIX)
    if not os.path.isdir(base):
        return []
    return sorted(int(y) for y in os.listdir(base) if y.isdigit())


def _source(table: str, year: Optional[int] = None) -> str:
    base = _sync()
    if year is None:
        return f"read_parquet('{base}/{PARQUET_PREFIX}/*/{table}.parquet')"
    return f"read_parquet('{base}/{PARQUET_PREFIX}/{year}/{table}.parquet')"


def _teams_source() -> str:
    base = _sync()
    year = max(_years())
    return f"read_parquet('{base}/{PARQUET_PREFIX}/{year}/teams.parquet')"


def _query(
    model_cls: Type[Model],
    orm_type: Type[ModelORM],
    source: str,
    where: List[str],
    params: List[Any],
    metric: Optional[str] = None,
    ascending: Optional[bool] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[Model]:
    cols = columns(orm_type)
    sql = f"SELECT * FROM {source}"
    clauses = list(where)
    if metric is not None:
        if metric not in {c[0] for c in cols}:
            raise ValueError(f"invalid metric: {metric}")
        clauses.append(f'"{metric}" IS NOT NULL')
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if metric is not None:
        direction = "ASC" if ascending else "DESC"
        sql += f' ORDER BY "{metric}" {direction}'
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    if offset is not None:
        sql += f" OFFSET {int(offset)}"

    cursor = _connection().cursor()
    cursor.execute(sql, params)
    names = [d[0] for d in cursor.description]
    return [
        from_row(model_cls, cols, dict(zip(names, row))) for row in cursor.fetchall()
    ]


def _one(
    model_cls: Type[Model],
    orm_type: Type[ModelORM],
    source: str,
    where: List[str],
    params: List[Any],
) -> Optional[Model]:
    rows = _query(model_cls, orm_type, source, where, params, limit=1)
    return rows[0] if rows else None


def get_team(team: int) -> Optional[Model]:
    model, orm = SPECS["teams"]
    return _one(model, orm, _teams_source(), ['"team" = ?'], [team])


def get_teams(
    country: Optional[str] = None,
    state: Optional[str] = None,
    district: Optional[str] = None,
    active: Optional[bool] = None,
    metric: Optional[str] = None,
    ascending: Optional[bool] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[Model]:
    model, orm = SPECS["teams"]
    where, params = _eq(
        {"country": country, "state": state, "district": district, "active": active}
    )
    return _query(
        model, orm, _teams_source(), where, params, metric, ascending, limit, offset
    )


def get_year(year: int) -> Optional[Model]:
    model, orm = SPECS["year"]
    return _one(model, orm, _source("year", year), ['"year" = ?'], [year])


def get_years(
    metric: Optional[str] = None,
    ascending: Optional[bool] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[Model]:
    model, orm = SPECS["year"]
    return _query(model, orm, _source("year"), [], [], metric, ascending, limit, offset)


def get_team_year(team: int, year: int) -> Optional[Model]:
    model, orm = SPECS["team_years"]
    return _one(
        model,
        orm,
        _source("team_years", year),
        ['"team" = ?', '"year" = ?'],
        [team, year],
    )


def get_team_years(
    team: Optional[int] = None,
    teams: Optional[List[str]] = None,
    year: Optional[int] = None,
    country: Optional[str] = None,
    state: Optional[str] = None,
    district: Optional[str] = None,
    metric: Optional[str] = None,
    ascending: Optional[bool] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[Model]:
    model, orm = SPECS["team_years"]
    where, params = _eq(
        {
            "team": team,
            "year": year,
            "country": country,
            "state": state,
            "district": district,
        }
    )
    if teams is not None:
        where.append(f'"team" IN ({", ".join("?" for _ in teams)})')
        params.extend(teams)
    return _query(
        model,
        orm,
        _source("team_years", year),
        where,
        params,
        metric,
        ascending,
        limit,
        offset,
    )


def get_event(event_id: str) -> Optional[Model]:
    model, orm = SPECS["events"]
    return _one(model, orm, _source("events"), ['"key" = ?'], [event_id])


def get_events(
    year: Optional[int] = None,
    country: Optional[str] = None,
    state: Optional[str] = None,
    district: Optional[str] = None,
    type: Optional[str] = None,
    week: Optional[int] = None,
    metric: Optional[str] = None,
    ascending: Optional[bool] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[Model]:
    model, orm = SPECS["events"]
    where, params = _eq(
        {
            "year": year,
            "country": country,
            "state": state,
            "district": district,
            "type": type,
            "week": week,
        }
    )
    return _query(
        model,
        orm,
        _source("events", year),
        where,
        params,
        metric,
        ascending,
        limit,
        offset,
    )


def get_team_event(team: int, event: str) -> Optional[Model]:
    model, orm = SPECS["team_events"]
    return _one(
        model, orm, _source("team_events"), ['"team" = ?', '"event" = ?'], [team, event]
    )


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
) -> List[Model]:
    model, orm = SPECS["team_events"]
    where, params = _eq(
        {
            "team": team,
            "year": year,
            "event": event,
            "country": country,
            "state": state,
            "district": district,
            "type": type,
            "week": week,
        }
    )
    if teams is not None:
        where.append(f'"team" IN ({", ".join("?" for _ in teams)})')
        params.extend(teams)
    return _query(
        model,
        orm,
        _source("team_events", year),
        where,
        params,
        metric,
        ascending,
        limit,
        offset,
    )


def get_match(match: str) -> Optional[Model]:
    model, orm = SPECS["matches"]
    return _one(model, orm, _source("matches"), ['"key" = ?'], [match])


def get_matches(
    team: Optional[int] = None,
    year: Optional[int] = None,
    event: Optional[str] = None,
    week: Optional[int] = None,
    elim: Optional[bool] = None,
    metric: Optional[str] = None,
    ascending: Optional[bool] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[Model]:
    model, orm = SPECS["matches"]
    where, params = _eq({"year": year, "event": event, "week": week, "elim": elim})
    if team is not None:
        cols = ["red_1", "red_2", "red_3", "blue_1", "blue_2", "blue_3"]
        where.append("(" + " OR ".join(f'"{c}" = ?' for c in cols) + ")")
        params.extend([team] * len(cols))
    return _query(
        model,
        orm,
        _source("matches", year),
        where,
        params,
        metric,
        ascending,
        limit,
        offset,
    )


def _eq(filters: Dict[str, Any]) -> Tuple[List[str], List[Any]]:
    where: List[str] = []
    params: List[Any] = []
    for name, value in filters.items():
        if value is not None:
            where.append(f'"{name}" = ?')
            params.append(value)
    return where, params
