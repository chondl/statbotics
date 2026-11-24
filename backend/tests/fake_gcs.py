"""In-memory stand-in for the google-cloud-storage Bucket/Blob surface the
backend uses. One shared fake for every test that touches the bucket.

Semantics mirror google-cloud-storage 3.1.0 as far as the backend relies on
them:

- every write assigns a new, increasing ``generation``; old generations stay
  readable through ``bucket.blob(name, generation=g)`` (object versioning);
- ``bucket.get_blob(name)`` returns None when the object does not exist;
- ``upload_from_string(..., if_generation_match=g)`` raises
  ``PreconditionFailed`` unless the live generation is ``g`` (``0`` means
  "must not exist");
- a download of a missing object (or missing generation) raises ``NotFound``;
- ``blob.md5_hash`` is the base64 of the binary MD5 of the stored bytes.

Hooks for fault injection: ``bucket.before_download`` and
``bucket.before_upload`` are lists of callables ``(blob) -> None`` run before
each download / upload; a hook may raise, or write to the bucket through
``bucket.put`` to simulate a concurrent writer.
"""

import base64
import hashlib
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from google.api_core.exceptions import NotFound, PreconditionFailed


class FakeBlob:
    def __init__(
        self, bucket: "FakeBucket", name: str, generation: Optional[int] = None
    ):
        self.bucket = bucket
        self.name = name
        self.generation = generation
        self.cache_control: Optional[str] = None
        self.content_encoding: Optional[str] = None
        self.content_type: Optional[str] = None

    # ------------------------------------------------------------- reads
    def exists(self) -> bool:
        return self.name in self.bucket.objects

    def download_as_bytes(self) -> bytes:
        for hook in list(self.bucket.before_download):
            hook(self)
        self.bucket.downloads.append((self.name, self.generation))
        if self.generation is None:
            if self.name not in self.bucket.objects:
                raise NotFound(f"No such object: {self.name}")
            return self.bucket.objects[self.name]
        key = (self.name, self.generation)
        if key not in self.bucket.history:
            raise NotFound(f"No such object: {self.name}#{self.generation}")
        return self.bucket.history[key]

    @property
    def md5_hash(self) -> Optional[str]:
        data = self.bucket.objects.get(self.name)
        if data is None:
            return None
        return base64.b64encode(hashlib.md5(data).digest()).decode("ascii")

    @property
    def size(self) -> Optional[int]:
        data = self.bucket.objects.get(self.name)
        return None if data is None else len(data)

    @property
    def time_created(self) -> Optional[datetime]:
        return self.bucket.created.get(self.name)

    # ------------------------------------------------------------ writes
    def upload_from_string(
        self,
        data: Any,
        content_type: str = "",
        if_generation_match: Optional[int] = None,
    ) -> None:
        for hook in list(self.bucket.before_upload):
            hook(self)
        if isinstance(data, str):
            data = data.encode("utf-8")
        if if_generation_match is not None:
            live = self.bucket.generation_of(self.name)
            if live != if_generation_match:
                raise PreconditionFailed(
                    f"{self.name}: generation {live} != {if_generation_match}"
                )
        self.bucket.put(
            self.name,
            data,
            cache_control=self.cache_control,
            content_encoding=self.content_encoding,
            content_type=content_type or None,
        )
        self.bucket.uploads.append(self.name)
        self.generation = self.bucket.generation_of(self.name)

    def delete(self) -> None:
        self.bucket.objects.pop(self.name, None)
        self.bucket.generations.pop(self.name, None)


class FakeBucket:
    def __init__(self) -> None:
        # Live bytes by object name. Tests may seed this dict directly; such
        # objects get a generation on first use.
        self.objects: Dict[str, bytes] = {}
        self.generations: Dict[str, int] = {}
        self.history: Dict[Tuple[str, int], bytes] = {}
        self.metadata: Dict[str, Dict[str, Optional[str]]] = {}
        self.created: Dict[str, datetime] = {}
        self.uploads: List[str] = []
        self.downloads: List[Tuple[str, Optional[int]]] = []
        self.before_download: List[Callable[[FakeBlob], None]] = []
        self.before_upload: List[Callable[[FakeBlob], None]] = []
        self._next_generation = 1

    # ----------------------------------------------------------- helpers
    def _new_generation(self) -> int:
        g = self._next_generation
        self._next_generation += 1
        return g

    def generation_of(self, name: str) -> int:
        """Live generation of ``name``; 0 when it does not exist."""
        if name not in self.objects:
            return 0
        if name not in self.generations:
            # Seeded directly through ``objects``.
            g = self._new_generation()
            self.generations[name] = g
            self.history[(name, g)] = self.objects[name]
        return self.generations[name]

    def put(self, name: str, data: bytes, **metadata: Optional[str]) -> int:
        """Write ``data`` unconditionally (bypassing hooks); returns the new
        generation. Simulates another writer."""
        g = self._new_generation()
        self.objects[name] = data
        self.generations[name] = g
        self.history[(name, g)] = data
        self.metadata[name] = dict(metadata)
        self.created.setdefault(name, datetime.now(timezone.utc))
        return g

    # ------------------------------------------------- google API surface
    def blob(self, name: str, generation: Optional[int] = None) -> FakeBlob:
        return FakeBlob(self, name, generation)

    def get_blob(self, name: str) -> Optional[FakeBlob]:
        if name not in self.objects:
            return None
        blob = FakeBlob(self, name, self.generation_of(name))
        meta = self.metadata.get(name, {})
        blob.cache_control = meta.get("cache_control")
        blob.content_encoding = meta.get("content_encoding")
        blob.content_type = meta.get("content_type")
        return blob

    def list_blobs(self, prefix: str = "") -> List[FakeBlob]:
        return [self.blob(n) for n in sorted(self.objects) if n.startswith(prefix)]

    def delete_blobs(self, blobs: List[FakeBlob]) -> None:
        for b in blobs:
            b.delete()

    def copy_blob(self, blob: FakeBlob, bucket: "FakeBucket", new_name: str) -> None:
        bucket.put(new_name, self.objects[blob.name], **self.metadata.get(blob.name, {}))
