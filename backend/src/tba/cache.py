"""GCS-persisted TBA cache: per-year archives + an etag manifest.

Design: docs/superpowers/specs/2026-07-20-tba-cache-design.md (§2.1–§2.4).

One tar.gz per year plus one global archive live under ``tba-cache/`` in the
site bucket. Each archive holds the pickle files in the local
``TBA_CACHE_DIR/<url>/data.p`` layout plus a ``manifest.json`` mapping
cache-key URL -> {etag, last_validated}. The in-memory manifest here is the
storage layer's etag source; the pipeline's objs[5]/DB etag flow is
dual-written and untouched.

Failure honesty: the cache is an optimization. hydrate() and persist() never
raise — GCS trouble logs loudly and the pipeline runs cold. An archive whose
hydrate *failed* (as opposed to simply not existing yet) is never persisted,
so a partial local tree cannot clobber a fuller stored archive.
"""

import io
import json
import os
import re
import tarfile
import traceback
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set

ARCHIVE_PREFIX = "tba-cache"
GLOBAL_ARCHIVE = "global"
MANIFEST_NAME = "manifest.json"

# Process-local state. url -> {"etag": ..., "last_validated": ...}
_manifest: Dict[str, Dict[str, str]] = {}
_hydrated: Set[str] = set()  # archive names already hydrated this process
_dirty: Set[str] = set()  # archive names needing persist
_blocked: Set[str] = set()  # hydrate errored (not just missing): never persist

_YEAR_RE = re.compile(r"^\d{4}$")
_YEAR_PREFIX_RE = re.compile(r"^(\d{4})")


def reset_state() -> None:
    """Clear all process-local cache state (tests)."""
    _manifest.clear()
    _hydrated.clear()
    _dirty.clear()
    _blocked.clear()


def archive_for(url: str) -> str:
    """Which archive a cache-key URL belongs to: a 4-digit year or "global".

    URL families (verified as the only ones fetched anywhere):
    - events/{year}, districts/{year} -> year from the path segment
    - event/{key}/..., district/{key}/teams -> 4-digit year prefix of the key
    - teams/{page} -> global
    Unknown families fall back to global (safe default) with a warning.
    """
    parts = url.strip("/").split("/")
    family = parts[0] if parts else ""
    if family in ("events", "districts") and len(parts) == 2:
        if _YEAR_RE.match(parts[1]):
            return parts[1]
    if family in ("event", "district") and len(parts) >= 2:
        match = _YEAR_PREFIX_RE.match(parts[1])
        if match is not None:
            return match.group(1)
    if family == "teams":
        return GLOBAL_ARCHIVE
    print(f"WARNING: unknown TBA cache key family for {url}; using global archive")
    return GLOBAL_ARCHIVE


def _cache_dir() -> str:
    # Late import: src.tba.main imports this module at module level.
    import src.tba.main as tba_main

    return tba_main.TBA_CACHE_DIR


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


"""
MANIFEST (design §2.4, §3.3)
"""


def stored_etag(url: str) -> Optional[str]:
    entry = _manifest.get(url)
    return entry.get("etag") if entry is not None else None


def record_success(url: str, etag: Optional[str]) -> None:
    """A 200 rewrote the pickle: record the new etag and a fresh
    last_validated. Called on every 200 regardless of who supplied the
    request etag, so the manifest stays authoritative independent of the
    hit/miss path (fixes design §3.3)."""
    if etag is None:
        # No etag on the response: any stored etag no longer describes the
        # pickle just written.
        _manifest.pop(url, None)
    else:
        _manifest[url] = {"etag": etag, "last_validated": _now_iso()}
    _dirty.add(archive_for(url))


def record_not_modified(url: str, etag: Optional[str]) -> None:
    """A 304 confirmed the request etag. Refresh last_validated only when
    that etag is the one we have stored (the pickle's etag) — a 304 against
    a caller-supplied foreign etag says nothing about our pickle."""
    entry = _manifest.get(url)
    if etag is None or entry is None or entry.get("etag") != etag:
        return
    entry["last_validated"] = _now_iso()
    _dirty.add(archive_for(url))


"""
ARCHIVES (design §2.1)
"""


def pack_archive(name: str, root: str) -> bytes:
    """tar.gz of every cached pickle under root belonging to archive `name`,
    plus manifest.json restricted to that archive's keys."""
    buf = io.BytesIO()
    manifest = {
        url: dict(entry) for url, entry in _manifest.items() if archive_for(url) == name
    }
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for dirpath, _dirs, files in os.walk(root):
            if "data.p" not in files:
                continue
            url = os.path.relpath(dirpath, root).replace(os.sep, "/")
            if archive_for(url) != name:
                continue
            tar.add(os.path.join(dirpath, "data.p"), arcname=url + "/data.p")
        payload = json.dumps(manifest, sort_keys=True).encode("utf-8")
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


def extract_archive(raw: bytes, dest: str) -> Dict[str, Dict[str, str]]:
    """Extract an archive's pickles into dest; return its manifest dict.
    Member paths are validated (regular files, no absolute/.. escapes)."""
    manifest: Dict[str, Dict[str, str]] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            name = member.name.lstrip("./")
            if name.startswith("/") or ".." in name.split("/"):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            payload = handle.read()
            if name == MANIFEST_NAME:
                manifest = json.loads(payload)
                continue
            path = os.path.join(dest, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as out:
                out.write(payload)
    return manifest


"""
GCS I/O — isolated for tests; late imports keep import order safe
"""


def _archive_object(name: str) -> str:
    return f"{ARCHIVE_PREFIX}/{name}.tar.gz"


def _download_archive(name: str) -> Optional[bytes]:
    """Bytes of the stored archive, or None if it does not exist yet."""
    from google.cloud.exceptions import NotFound
    from src.google.storage import _bucket

    try:
        return _bucket().blob(_archive_object(name)).download_as_bytes()
    except NotFound:
        return None


def _upload_archive(name: str, data: bytes) -> None:
    """Atomic single-object PUT of one archive."""
    from src.google.storage import _bucket, _upload_bytes

    _upload_bytes(_bucket(), _archive_object(name), data, None)


"""
HYDRATE / PERSIST (design §2.2)
"""


def _gcs_disabled() -> bool:
    from src.constants import DISABLE_GCS

    return DISABLE_GCS


def hydrate(year: Optional[int] = None) -> None:
    """Ensure the global archive (and the year's archive, if given) are
    extracted into TBA_CACHE_DIR, once per process. GCS only — adds zero TBA
    requests. Missing archive = empty cache (cold start, self-heals). Never
    raises."""
    if _gcs_disabled():
        return
    names = [GLOBAL_ARCHIVE] + ([str(year)] if year is not None else [])
    for name in names:
        if name in _hydrated:
            continue
        try:
            raw = _download_archive(name)
            if raw is None:
                print(f"TBA cache: no archive for {name} yet (cold start)")
            else:
                manifest = extract_archive(raw, _cache_dir())
                # Entries recorded this process are fresher than the archive.
                for url, entry in manifest.items():
                    _manifest.setdefault(url, entry)
                print(f"TBA cache: hydrated {name} ({len(manifest)} keys)")
        except Exception:
            # Run cold, and block persist for this archive so a partial local
            # tree cannot clobber the stored one.
            _blocked.add(name)
            print(f"WARNING: TBA cache hydrate failed for {name}; running cold")
            traceback.print_exc()
        _hydrated.add(name)


def persist() -> None:
    """Upload every dirty archive (re-packed from the local cache dir).
    Dirty-only, atomic per archive, never raises; a failed upload stays
    dirty for a later retry."""
    if _gcs_disabled():
        return
    for name in sorted(_dirty):
        if name in _blocked:
            print(
                f"WARNING: not persisting TBA cache {name}: "
                "its hydrate failed this process"
            )
            continue
        try:
            _upload_archive(name, pack_archive(name, _cache_dir()))
            print(f"TBA cache: persisted {name}")
        except Exception:
            print(f"WARNING: failed to persist TBA cache archive {name}")
            traceback.print_exc()
            continue
        _dirty.discard(name)


__all__: List[str] = [
    "archive_for",
    "extract_archive",
    "hydrate",
    "pack_archive",
    "persist",
    "record_not_modified",
    "record_success",
    "reset_state",
    "stored_etag",
]
