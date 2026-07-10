import io
from typing import Any, List, Type

import pyarrow as pa
import pyarrow.parquet as pq

from src.data.utils import objs_type
from src.db.models import Team
from src.db.models.event import EventORM
from src.db.models.main import Model, ModelORM
from src.db.models.match import MatchORM
from src.db.models.team import TeamORM
from src.db.models.team_event import TeamEventORM
from src.db.models.team_year import TeamYearORM
from src.db.models.year import YearORM
from src.db_duckdb.schema import PARQUET_PREFIX, columns, to_row
from src.google.publish import Manifest, content_hash, versioned_key
from src.google.storage import IMMUTABLE_CACHE, _bucket, read_manifest, write_manifest

ARROW_TYPES = {
    "int": pa.int64(),
    "float": pa.float64(),
    "bool": pa.bool_(),
    "str": pa.string(),
    "enum": pa.string(),
    "json": pa.string(),
}


def parquet_logical(year: int, table: str) -> str:
    return f"{PARQUET_PREFIX}/{year}/{table}.parquet"


def serialize_table(objs: List[Model], orm_type: Type[ModelORM]) -> bytes:
    cols = columns(orm_type)
    rows = [to_row(o, cols) for o in sorted(objs, key=lambda o: o.pk())]
    columns_data = list(zip(*rows)) if rows else [() for _ in cols]
    arrays = [
        pa.array(list(values), type=ARROW_TYPES[kind])
        for (_, kind, _), values in zip(cols, columns_data)
    ]
    table = pa.Table.from_arrays(arrays, names=[name for name, _, _ in cols])
    buf = io.BytesIO()
    pq.write_table(table, buf)
    return buf.getvalue()


def write_parquet(year: int, objs: objs_type, teams: List[Team]) -> None:
    sources: List[Any] = [
        ("team_years", list(objs[1].values()), TeamYearORM),
        ("events", list(objs[2].values()), EventORM),
        ("team_events", list(objs[3].values()), TeamEventORM),
        ("matches", list(objs[4].values()), MatchORM),
        ("teams", teams, TeamORM),
        ("year", [objs[0]], YearORM),
    ]

    bucket = _bucket()
    manifest = read_manifest() or Manifest()
    blobs = dict(manifest.blobs)
    changed = False
    for table, rows, orm_type in sources:
        data = serialize_table(rows, orm_type)
        logical = parquet_logical(year, table)
        digest = content_hash(data)
        if manifest.hash_for(logical) == digest:
            continue
        key = versioned_key(logical, digest)
        blob = bucket.blob(key)
        blob.cache_control = IMMUTABLE_CACHE
        blob.upload_from_string(data, "application/octet-stream")
        blobs[logical] = key
        changed = True

    if changed:
        manifest.blobs = blobs
        write_manifest(manifest, bucket)
