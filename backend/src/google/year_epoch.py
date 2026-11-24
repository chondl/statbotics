"""Per-year hist epochs for past years (trailing-year refresh spec §8).

A past year's site blobs are served from ``hist/{e}/{path}``, where ``e`` is
the year's effective epoch, ``max(hist_epochs[year] ?? 0, hist_epoch)``.
Those objects are immutable and cached for a year, so a reprocess that
changes a page must never overwrite one in place: ``publish_past_year``
writes the whole year under a fresh epoch ``e + 1`` and only then switches
the year to it in the manifest. Readers therefore always resolve one
complete, consistent epoch, and a crash before the switch leaves the old
epoch serving and the next run redoing the advance.
"""

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List

from src.data import trailing
from src.google import storage
from src.google.publish import Manifest, historical_key
from src.google.year_path import effective_hist_epoch, year_from_path

UNCHANGED = "unchanged"
GAP_FILLED = "gap_filled"
ADVANCED = "advanced"

_MISSING = "missing"
_SAME = "same"
_DIFFERS = "differs"


def _md5_b64(data: bytes) -> str:
    # GCS reports Blob.md5_hash as the base64 of the binary MD5.
    return base64.b64encode(hashlib.md5(data).digest()).decode("ascii")


def _shadowing_keys(manifest: Manifest, year: int) -> List[str]:
    """manifest.blobs entries for ``year`` that would outrank its hist blobs
    (frontend resolution checks manifest.blobs first). Parquet entries are
    not site blobs and stay."""
    return [
        k
        for k in manifest.blobs
        if year_from_path(k) == year and not k.startswith("parquet/")
    ]


def _upload_all(bucket: Any, epoch: int, rendered: Dict[str, bytes]) -> None:
    def upload(item: Any) -> None:
        path, data = item
        blob = bucket.blob(historical_key(epoch, path))
        blob.cache_control = storage.IMMUTABLE_CACHE
        blob.upload_from_string(data, "application/octet-stream")

    with ThreadPoolExecutor() as executor:
        list(executor.map(upload, rendered.items()))


def publish_past_year(year: int, rendered: Dict[str, bytes], bucket: Any = None) -> str:
    """Publish one past year's rendered site blobs (logical path -> bytes)
    so the pages serve exactly ``rendered``. Returns the outcome:

    - ``"unchanged"``: every path already holds these bytes under the
      effective epoch ``e``; nothing uploaded.
    - ``"gap_filled"``: no path exists under ``e`` (a cold year); all are
      uploaded under ``e``. No manifest change is needed for that.
    - ``"advanced"``: some path is missing or differs under ``e``; all are
      uploaded under ``e + 1`` (overwriting any leftovers of a crashed
      advance), then ``hist_epochs[str(year)] = e + 1``.

    In every outcome the year's non-Parquet manifest.blobs entries, which
    would shadow the hist blobs, are removed in the same manifest write; the
    manifest is not written when there is nothing to change. Refuses a live
    year (no hist blob is ever written for one)."""
    if trailing.is_live_year(year):
        raise ValueError("live year")
    bucket = bucket or storage._bucket()

    # A ManifestReadError propagates before any upload: without the manifest
    # the effective epoch is unknown.
    manifest = storage.read_manifest(bucket) or Manifest()
    e = effective_hist_epoch(manifest, year)

    def probe(item: Any) -> str:
        path, data = item
        blob = bucket.get_blob(historical_key(e, path))
        if blob is None:
            return _MISSING
        return _SAME if blob.md5_hash == _md5_b64(data) else _DIFFERS

    with ThreadPoolExecutor() as executor:
        states = list(executor.map(probe, rendered.items()))

    if states and all(s == _MISSING for s in states):
        outcome, epoch = GAP_FILLED, e
    elif any(s != _SAME for s in states):
        outcome, epoch = ADVANCED, e + 1
    else:
        outcome, epoch = UNCHANGED, e

    if outcome != UNCHANGED:
        _upload_all(bucket, epoch, rendered)

    # Uploads first, manifest last: the switch to e + 1 is only ever
    # published over a complete upload set.
    if outcome == ADVANCED or _shadowing_keys(manifest, year):

        def mutate(m: Manifest) -> None:
            for key in _shadowing_keys(m, year):
                m.blobs.pop(key, None)
            if outcome == ADVANCED:
                m.hist_epochs[str(year)] = epoch

        storage.update_manifest(mutate, bucket)

    print(f"YEAR EPOCH {year} {outcome} epoch={epoch}")
    return outcome
