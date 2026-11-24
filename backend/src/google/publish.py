import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Set

from src.google.year_path import effective_hist_epoch, year_from_path

__all__ = [
    "MANIFEST_OBJECT",
    "Manifest",
    "UploadPlan",
    "content_hash",
    "effective_hist_epoch",
    "historical_key",
    "plan_uploads",
    "versioned_key",
    "year_from_path",
]

MANIFEST_OBJECT = "manifest.json"
VERSION_PREFIX = "v2"
HIST_PREFIX = "hist"
HASH_LEN = 12
SCHEMA = 1

_KNOWN_FIELDS = {
    "schema",
    "cycle",
    "hist_epoch",
    "blobs",
    "trailing_year",
    "hist_epochs",
    "chain_pending",
}


def content_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:HASH_LEN]


def versioned_key(logical_path: str, digest: str) -> str:
    return f"{VERSION_PREFIX}/{logical_path}.{digest}"


def historical_key(epoch: int, logical_path: str) -> str:
    return f"{HIST_PREFIX}/{epoch}/{logical_path}"


@dataclass
class Manifest:
    schema: int = SCHEMA
    cycle: str = ""
    hist_epoch: int = 1
    blobs: Dict[str, str] = field(default_factory=dict)
    # The trailing year while its window is open (bootstrap marker and the
    # frontend's live-year source); None otherwise.
    trailing_year: Optional[int] = None
    # Year (string) -> epoch, each above hist_epoch. Correctness-critical:
    # every writer carries it forward (storage.update_manifest).
    hist_epochs: Dict[str, int] = field(default_factory=dict)
    # A past or trailing year's parquet/{Y}/team_years digest changed and the
    # chained current-year re-render (team pages embed every season) has not
    # yet published. Set in the same manifest write that changed the digest;
    # cleared by the full current-year publish; the gate schedules
    # GET /v3/data/reset_curr_year while it is set. Survives a crash between
    # the publish and the chain.
    chain_pending: bool = False
    # Unknown top-level keys, carried through untouched so an older writer
    # never strips a newer writer's fields.
    extra: Dict[str, Any] = field(default_factory=dict)

    def hash_for(self, logical_path: str) -> Optional[str]:
        key = self.blobs.get(logical_path)
        if key is None:
            return None
        return key.rsplit(".", 1)[-1]

    def to_json(self) -> str:
        data: Dict[str, Any] = dict(self.extra)
        data.update(
            {
                "schema": self.schema,
                "cycle": self.cycle,
                "hist_epoch": self.hist_epoch,
                "blobs": self.blobs,
                "trailing_year": self.trailing_year,
                "hist_epochs": self.hist_epochs,
                "chain_pending": self.chain_pending,
            }
        )
        return json.dumps(data, sort_keys=True)

    @classmethod
    def from_json(cls, raw: object) -> "Manifest":
        if isinstance(raw, (bytes, bytearray)):
            raw = bytes(raw).decode("utf-8")
        if isinstance(raw, str):
            data = json.loads(raw)
        elif isinstance(raw, dict):
            data = raw
        else:
            raise TypeError(f"Cannot parse manifest from {type(raw)!r}")
        if not isinstance(data, dict):
            raise TypeError(f"Manifest JSON is {type(data)!r}, not an object")
        trailing = data.get("trailing_year")
        return cls(
            schema=int(data.get("schema", SCHEMA)),
            cycle=str(data.get("cycle", "")),
            hist_epoch=int(data.get("hist_epoch", 1)),
            blobs=dict(data.get("blobs") or {}),
            trailing_year=None if trailing is None else int(trailing),
            hist_epochs={
                str(k): int(v) for k, v in (data.get("hist_epochs") or {}).items()
            },
            chain_pending=bool(data.get("chain_pending", False)),
            extra={k: v for k, v in data.items() if k not in _KNOWN_FIELDS},
        )


@dataclass
class UploadPlan:
    """Objects to upload, then the manifest delta to commit through
    storage.update_manifest. The delta, not a whole manifest: a whole-manifest
    write planned from a stale read would erase concurrent writers' entries."""

    uploads: Dict[str, bytes]
    legacy_uploads: Dict[str, bytes]
    blob_updates: Dict[str, str] = field(default_factory=dict)
    blob_removals: Set[str] = field(default_factory=set)

    def apply(self, manifest: Manifest) -> None:
        """Idempotent: removals first, then updates."""
        for logical in self.blob_removals:
            manifest.blobs.pop(logical, None)
        manifest.blobs.update(self.blob_updates)


def plan_uploads(
    rendered: Dict[str, bytes],
    prev: Optional[Manifest],
    cycle: str,
) -> UploadPlan:
    """Upload and record only blobs whose digest differs from ``prev``.
    ``cycle`` is kept for callers; the manifest's cycle is stamped by
    update_manifest at commit time."""
    prev = prev or Manifest()

    uploads: Dict[str, bytes] = {}
    legacy_uploads: Dict[str, bytes] = {}
    blob_updates: Dict[str, str] = {}

    for logical_path, data in rendered.items():
        digest = content_hash(data)
        if prev.hash_for(logical_path) != digest:
            key = versioned_key(logical_path, digest)
            uploads[key] = data
            legacy_uploads[logical_path] = data
            blob_updates[logical_path] = key

    return UploadPlan(
        uploads=uploads, legacy_uploads=legacy_uploads, blob_updates=blob_updates
    )
