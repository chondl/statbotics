from datetime import datetime
import math
from typing import Any, Dict, Tuple

import attr

from src.db.models import ETag, Event, Match, TeamEvent, TeamYear, Year

objs_type = Tuple[
    Year,
    Dict[str, TeamYear],
    Dict[str, Event],
    Dict[str, TeamEvent],
    Dict[str, Match],
    Dict[str, ETag],
]


def create_objs(year: int) -> objs_type:
    return (Year(year=year), {}, {}, {}, {}, {})


def _canonical(value: Any) -> Any:
    if isinstance(value, float):
        return "__nan__" if math.isnan(value) else value
    if attr.has(type(value)):
        return {
            f.name: _canonical(getattr(value, f.name)) for f in attr.fields(type(value))
        }
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_canonical(v) for v in value]
    return value


# nan_safe_eq outlived the DB: src/google/storage.py uses it as the site-blob
# change gate (DB retirement Phase 4 deleted read_objs/write_objs and the
# `changed()` DB-diff helper that was nested inside write_objs).
def nan_safe_eq(a: Any, b: Any) -> bool:
    if a == b:
        return True
    return _canonical(a) == _canonical(b)


class Timer:
    def __init__(self):
        self.start = datetime.now()

    def print(self, label: str) -> None:
        end = datetime.now()
        print(label, "\t", end - self.start)
        self.start = end
