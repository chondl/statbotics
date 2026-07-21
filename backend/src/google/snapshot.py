import hashlib
import json
import os
import pickle
import zlib
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple, Type
from uuid import uuid4

import attr
import zstandard
from sqlalchemy import inspect
from sqlalchemy.sql.sqltypes import Enum as SQLEnum

from src.data.utils import objs_type
from src.db.models import ETag, Event, Match, Team, TeamEvent, TeamYear, Year
from src.db.models.etag import ETagORM
from src.db.models.event import EventORM
from src.db.models.main import Model, ModelORM
from src.db.models.match import MatchORM
from src.db.models.team import TeamORM
from src.db.models.team_event import TeamEventORM
from src.db.models.team_year import TeamYearORM
from src.db.models.year import YearORM
from src.google.storage import _bucket

SNAPSHOT_SCHEMA = 2
LEGACY_SNAPSHOT_SCHEMA = 1
SNAPSHOT_PREFIX = "state"

_ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
_ZLIB_MAGIC = b"\x78"


def snapshot_key(year: int) -> str:
    return f"{SNAPSHOT_PREFIX}/snapshot.{year}"


def _models_fingerprint() -> str:
    # Guards against unpickling a snapshot written with a different model
    # shape: pickle.loads can silently succeed on slotted attrs classes after
    # a field removal, so the payload carries an explicit field-list hash.
    fields = {
        cls.__name__: [f.name for f in attr.fields(cls)]
        for cls in (Year, TeamYear, Event, TeamEvent, Match, ETag, Team)
    }
    return hashlib.sha256(json.dumps(fields, sort_keys=True).encode()).hexdigest()[:16]


def _enum_fields(orm_type: Type[ModelORM]) -> Dict[str, Type[Enum]]:
    fields: Dict[str, Type[Enum]] = {}
    for column in inspect(orm_type).columns:
        if isinstance(column.type, SQLEnum) and column.type.enum_class is not None:
            fields[column.name] = column.type.enum_class
    return fields


def _load(
    model_cls: Type[Model], orm_type: Type[ModelORM], data: Dict[str, Any]
) -> Any:
    obj = model_cls.from_dict(data)
    for name, enum_cls in _enum_fields(orm_type).items():
        value = getattr(obj, name)
        if value is not None and not isinstance(value, enum_cls):
            setattr(obj, name, enum_cls(value))
    return obj


def _load_values(
    model_cls: Type[Model], orm_type: Type[ModelORM], rows: List[Dict[str, Any]]
) -> Dict[str, Any]:
    loaded = [_load(model_cls, orm_type, row) for row in rows]
    return {o.pk(): o for o in loaded}


def serialize(objs: objs_type, teams: List[Team]) -> bytes:
    payload = {
        "schema": SNAPSHOT_SCHEMA,
        "fingerprint": _models_fingerprint(),
        "year": objs[0].year,
        "teams": sorted(teams, key=lambda t: t.team),
        "objs": objs,
    }
    return zstandard.ZstdCompressor(level=3).compress(pickle.dumps(payload, protocol=5))


def deserialize(raw: bytes) -> Tuple[objs_type, List[Team]]:
    if raw[:4] == _ZSTD_MAGIC:
        return _deserialize_pickle(raw)
    if raw[:1] == _ZLIB_MAGIC:
        return _deserialize_legacy(raw)
    raise ValueError("unrecognized snapshot format")


def _deserialize_pickle(raw: bytes) -> Tuple[objs_type, List[Team]]:
    payload = pickle.loads(zstandard.ZstdDecompressor().decompress(raw))
    schema = payload.get("schema")
    if schema != SNAPSHOT_SCHEMA:
        raise ValueError(f"snapshot schema {schema} != expected {SNAPSHOT_SCHEMA}")
    fingerprint = payload.get("fingerprint")
    expected = _models_fingerprint()
    if fingerprint != expected:
        raise ValueError(
            f"snapshot model fingerprint {fingerprint} != expected {expected}"
        )
    return payload["objs"], payload["teams"]


def _deserialize_legacy(raw: bytes) -> Tuple[objs_type, List[Team]]:
    payload = json.loads(zlib.decompress(raw).decode("utf-8"))
    schema = payload.get("schema")
    if schema != LEGACY_SNAPSHOT_SCHEMA:
        raise ValueError(
            f"snapshot schema {schema} != expected {LEGACY_SNAPSHOT_SCHEMA}"
        )
    data = payload["objs"]
    objs: objs_type = (
        _load(Year, YearORM, data["year"]),
        _load_values(TeamYear, TeamYearORM, data["team_years"]),
        _load_values(Event, EventORM, data["events"]),
        _load_values(TeamEvent, TeamEventORM, data["team_events"]),
        _load_values(Match, MatchORM, data["matches"]),
        _load_values(ETag, ETagORM, data["etags"]),
    )
    teams = [_load(Team, TeamORM, row) for row in payload["teams"]]
    return objs, teams


def write_snapshot(year: int, objs: objs_type, teams: List[Team]) -> None:
    bucket = _bucket()
    key = snapshot_key(year)
    tmp_key = f"{key}.{os.getpid()}.{uuid4().hex}.tmp"
    bucket.blob(tmp_key).upload_from_string(
        serialize(objs, teams), "application/octet-stream"
    )
    bucket.copy_blob(bucket.blob(tmp_key), bucket, key)
    bucket.blob(tmp_key).delete()


def read_snapshot(year: int) -> Optional[Tuple[objs_type, List[Team]]]:
    try:
        raw = _bucket().blob(snapshot_key(year)).download_as_bytes()
    except Exception:
        return None
    try:
        return deserialize(raw)
    except Exception as e:
        print(f"snapshot unreadable, falling back to DB path: {e}")
        return None
