import os
import shutil
import tempfile
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple, Type

import duckdb

from src.constants import CURR_YEAR
from src.db.models.main import Model, ModelORM
from src.db.models.match import Match
from src.db_duckdb.schema import PARQUET_PREFIX, SPECS, columns, from_row
from src.types.enums import MatchStatus

SYNC_TTL = float(os.environ.get("DUCKDB_SYNC_TTL", "30"))

_con: Optional[duckdb.DuckDBPyConnection] = None
_cache_root: Optional[str] = None
_current_dir: Optional[str] = None
_prev_dir: Optional[str] = None
_keys: Dict[str, str] = {}
_manifest_generation: Optional[int] = None
_last_sync: float = 0.0
_sync_lock = threading.Lock()


def _connection() -> duckdb.DuckDBPyConnection:
    global _con
    if _con is None:
        _con = duckdb.connect()
    return _con


def _sync() -> str:
    global _cache_root, _current_dir, _prev_dir, _keys
    global _manifest_generation, _last_sync

    now = time.monotonic()
    if _current_dir is not None and _last_sync and now - _last_sync < SYNC_TTL:
        return _current_dir

    with _sync_lock:
        now = time.monotonic()
        if _current_dir is not None and _last_sync and now - _last_sync < SYNC_TTL:
            return _current_dir

        from src.google.publish import MANIFEST_OBJECT, Manifest
        from src.google.storage import _bucket

        if _cache_root is None:
            _cache_root = tempfile.mkdtemp(prefix="duckdb_parquet_")

        blob = _bucket().blob(MANIFEST_OBJECT)
        try:
            blob.reload()
        except Exception:
            _last_sync = now
            if _current_dir is None:
                _current_dir = tempfile.mkdtemp(prefix="gen_", dir=_cache_root)
            return _current_dir

        if blob.generation == _manifest_generation and _current_dir is not None:
            _last_sync = now
            return _current_dir

        manifest = Manifest.from_json(blob.download_as_bytes())
        entries = {
            logical: key
            for logical, key in manifest.blobs.items()
            if logical.startswith(PARQUET_PREFIX + "/")
        }

        gen_dir = tempfile.mkdtemp(prefix="gen_", dir=_cache_root)
        for logical, key in entries.items():
            dest = os.path.join(gen_dir, logical)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            prev = os.path.join(_current_dir, logical) if _current_dir else None
            if _keys.get(logical) == key and prev is not None and os.path.exists(prev):
                os.link(prev, dest)
            else:
                _bucket().blob(key).download_to_filename(dest)

        old_prev = _prev_dir
        _prev_dir = _current_dir
        _current_dir = gen_dir
        _keys = entries
        _manifest_generation = blob.generation
        _last_sync = now
        if old_prev is not None:
            shutil.rmtree(old_prev, ignore_errors=True)
        return _current_dir


def _years(base: str) -> List[int]:
    root = os.path.join(base, PARQUET_PREFIX)
    if not os.path.isdir(root):
        return []
    return sorted(int(y) for y in os.listdir(root) if y.isdigit())


def _source(base: str, table: str, year: Optional[int] = None) -> str:
    if year is None:
        return f"read_parquet('{base}/{PARQUET_PREFIX}/*/{table}.parquet')"
    return f"read_parquet('{base}/{PARQUET_PREFIX}/{year}/{table}.parquet')"


def _teams_source(base: str) -> str:
    years = _years(base)
    if not years:
        # No parquet yet (first db-less deploy): glob resolves to no files, which
        # _query degrades to an empty result instead of crashing on max([]).
        return f"read_parquet('{base}/{PARQUET_PREFIX}/*/teams.parquet')"
    return f"read_parquet('{base}/{PARQUET_PREFIX}/{max(years)}/teams.parquet')"


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
    return _one(model, orm, _teams_source(_sync()), ['"team" = ?'], [team])


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
        model,
        orm,
        _teams_source(_sync()),
        where,
        params,
        metric,
        ascending,
        limit,
        offset,
    )


def get_year(year: int) -> Optional[Model]:
    model, orm = SPECS["year"]
    return _one(model, orm, _source(_sync(), "year", year), ['"year" = ?'], [year])


def get_years(
    metric: Optional[str] = None,
    ascending: Optional[bool] = None,
    limit: Optional[int] = None,
    offset: Optional[int] = None,
) -> List[Model]:
    model, orm = SPECS["year"]
    return _query(
        model, orm, _source(_sync(), "year"), [], [], metric, ascending, limit, offset
    )


def get_team_year(team: int, year: int) -> Optional[Model]:
    model, orm = SPECS["team_years"]
    return _one(
        model,
        orm,
        _source(_sync(), "team_years", year),
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
        _source(_sync(), "team_years", year),
        where,
        params,
        metric,
        ascending,
        limit,
        offset,
    )


def get_event(event_id: str) -> Optional[Model]:
    model, orm = SPECS["events"]
    return _one(model, orm, _source(_sync(), "events"), ['"key" = ?'], [event_id])


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
        _source(_sync(), "events", year),
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
        model,
        orm,
        _source(_sync(), "team_events"),
        ['"team" = ?', '"event" = ?'],
        [team, event],
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
        _source(_sync(), "team_events", year),
        where,
        params,
        metric,
        ascending,
        limit,
        offset,
    )


def get_match(match: str) -> Optional[Model]:
    model, orm = SPECS["matches"]
    return _one(model, orm, _source(_sync(), "matches"), ['"key" = ?'], [match])


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
        _source(_sync(), "matches", year),
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


def _event_filters(
    country: Optional[str], state: Optional[str], district: Optional[str]
) -> Tuple[List[str], List[Any]]:
    where: List[str] = []
    params: List[Any] = []
    if country is not None:
        where.append("e.country = ?")
        params.append(country)
    if state is not None:
        where.append("e.state = ?")
        params.append(state)
    if district == "regionals":
        where.append("e.district IS NULL")
    elif district is not None:
        where.append("e.district = ?")
        params.append(district)
    return where, params


def _joined_matches(
    year: int, where: List[str], params: List[Any], order: str, limit: int
) -> List[Model]:
    model, orm = SPECS["matches"]
    cols = columns(orm)
    sql = (
        f"SELECT m.* FROM {_source('matches', year)} AS m "
        f"JOIN {_source('events', year)} AS e ON m.event = e.key "
        f"WHERE {' AND '.join(where)} ORDER BY {order} LIMIT {int(limit)}"
    )
    cursor = _connection().cursor()
    cursor.execute(sql, params)
    names = [d[0] for d in cursor.description]
    return [from_row(model, cols, dict(zip(names, row))) for row in cursor.fetchall()]


def get_noteworthy_matches(
    year: int,
    country: Optional[str],
    state: Optional[str],
    district: Optional[str],
    elim: Optional[bool],
    week: Optional[int],
) -> Dict[str, List[Match]]:
    where = ["m.year = ?", "m.status = ?"]
    params: List[Any] = [year, MatchStatus.COMPLETED.value]
    e_where, e_params = _event_filters(country, state, district)
    where += e_where
    params += e_params
    if elim is not None:
        where.append("m.elim = ?")
        params.append(elim)
    if week is not None:
        where.append("e.week = ?")
        params.append(week)

    red = "m.red_score" if year < 2016 else "m.red_no_foul"
    blue = "m.blue_score" if year < 2016 else "m.blue_no_foul"

    def top(order: str) -> List[Match]:
        return _joined_matches(year, where, params, order, 30)  # type: ignore

    out: Dict[str, List[Match]] = {
        "high_score": top(f"greatest({red}, {blue}) DESC, m.time ASC"),
        "combined_score": top(f"({red} + {blue}) DESC, m.time ASC"),
        "losing_score": top("least(m.red_score, m.blue_score) DESC, m.time ASC"),
    }
    if year >= 2016:
        out["high_auto_score"] = top("greatest(m.red_auto, m.blue_auto) DESC, m.time ASC")
        out["high_teleop_score"] = top(
            "greatest(m.red_teleop, m.blue_teleop) DESC, m.time ASC"
        )
        out["high_endgame_score"] = top(
            "greatest(m.red_endgame, m.blue_endgame) DESC, m.time ASC"
        )
    return out


def get_upcoming_matches(
    country: Optional[str],
    state: Optional[str],
    district: Optional[str],
    elim: Optional[bool],
    minutes: int,
    limit: int,
    metric: str,
) -> List[Tuple[Match, str]]:
    curr_timestamp = int(datetime.now().timestamp()) - 60 * 5
    if minutes == -1:
        minutes = 60 * 24 * 7

    where = [
        "m.year = ?",
        "m.status = ?",
        "m.predicted_time > ?",
        "m.predicted_time < ?",
        "m.event = e.key",
    ]
    params: List[Any] = [
        CURR_YEAR,
        MatchStatus.UPCOMING.value,
        curr_timestamp,
        curr_timestamp + 60 * minutes,
    ]
    e_where, e_params = _event_filters(country, state, district)
    where += e_where
    params += e_params
    if elim is not None:
        where.append("m.elim = ?")
        params.append(elim)

    exprs = {
        "max_epa": "greatest(m.epa_red_score_pred, m.epa_blue_score_pred)",
        "sum_epa": "(m.epa_red_score_pred + m.epa_blue_score_pred)",
        "diff_epa": "abs(m.epa_red_score_pred - m.epa_blue_score_pred)",
        "time": "m.time",
    }
    order = ""
    if metric in ("max_epa", "sum_epa"):
        order = f" ORDER BY {exprs[metric]} DESC"
    elif metric in ("time", "diff_epa"):
        order = f" ORDER BY {exprs[metric]} ASC"

    model, orm = SPECS["matches"]
    cols = columns(orm)
    sql = (
        f"SELECT m.*, e.name AS event_name "
        f"FROM {_source('matches', CURR_YEAR)} AS m, {_source('events', CURR_YEAR)} AS e "
        f"WHERE {' AND '.join(where)}{order} LIMIT {int(limit)}"
    )
    cursor = _connection().cursor()
    cursor.execute(sql, params)
    names = [d[0] for d in cursor.description]
    return [
        (from_row(model, cols, dict(zip(names, row))), dict(zip(names, row))["event_name"])
        for row in cursor.fetchall()
    ]
