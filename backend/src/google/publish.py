import hashlib
import json
from dataclasses import dataclass, field
from typing import Dict, Optional

MANIFEST_OBJECT = "manifest.json"
VERSION_PREFIX = "v2"
HIST_PREFIX = "hist"
HASH_LEN = 12
SCHEMA = 1


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

    def hash_for(self, logical_path: str) -> Optional[str]:
        key = self.blobs.get(logical_path)
        if key is None:
            return None
        return key.rsplit(".", 1)[-1]

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema": self.schema,
                "cycle": self.cycle,
                "hist_epoch": self.hist_epoch,
                "blobs": self.blobs,
            },
            sort_keys=True,
        )

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
        return cls(
            schema=int(data.get("schema", SCHEMA)),
            cycle=str(data.get("cycle", "")),
            hist_epoch=int(data.get("hist_epoch", 1)),
            blobs=dict(data.get("blobs", {})),
        )


@dataclass
class UploadPlan:
    uploads: Dict[str, bytes]
    legacy_uploads: Dict[str, bytes]
    manifest: Manifest


def plan_uploads(
    rendered: Dict[str, bytes],
    prev: Optional[Manifest],
    cycle: str,
    hist_epoch: Optional[int] = None,
) -> UploadPlan:
    prev = prev or Manifest()
    if hist_epoch is None:
        hist_epoch = prev.hist_epoch

    uploads: Dict[str, bytes] = {}
    legacy_uploads: Dict[str, bytes] = {}
    blobs: Dict[str, str] = dict(prev.blobs)

    for logical_path, data in rendered.items():
        digest = content_hash(data)
        blobs[logical_path] = versioned_key(logical_path, digest)
        if prev.hash_for(logical_path) != digest:
            uploads[versioned_key(logical_path, digest)] = data
            legacy_uploads[logical_path] = data

    manifest = Manifest(schema=SCHEMA, cycle=cycle, hist_epoch=hist_epoch, blobs=blobs)
    return UploadPlan(uploads=uploads, legacy_uploads=legacy_uploads, manifest=manifest)
