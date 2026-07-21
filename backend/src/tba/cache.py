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
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

ARCHIVE_PREFIX = "tba-cache"
GLOBAL_ARCHIVE = "global"
MANIFEST_NAME = "manifest.json"

# Reserved manifest key carrying per-archive metadata (not a cache-key URL):
# {"meta_persisted": <iso>} — when this archive was last uploaded for a
# metadata-only change. Stripped from the url manifest on hydrate.
ARCHIVE_META_KEY = "__archive__"

# Reserved manifest key (global archive only) carrying the historical
# sweep's round-robin cursor: {"next_year": "2013"} — the next year
# /v3/data/revalidate_tba will sweep (design §2.3). Stripped on hydrate,
# like ARCHIVE_META_KEY.
SWEEP_CURSOR_KEY = "__sweep__"

# Metadata-only dirtiness (a 304 refreshing last_validated) re-uploads an
# archive at most this often. last_validated is an optimization hint: losing
# a few hours of it costs extra 304s, never correctness.
META_PERSIST_INTERVAL_HOURS = 6

# Process-local state. url -> {"etag": ..., "last_validated": ...}
_manifest: Dict[str, Dict[str, str]] = {}
_hydrated: Set[str] = set()  # archive names already hydrated this process
_dirty: Set[str] = set()  # content-dirty: pickle written / etag changed
_meta_dirty: Set[str] = set()  # only last_validated changed (debounced)
_meta_persisted: Dict[str, str] = {}  # archive -> last metadata upload (iso)
_blocked: Set[str] = set()  # hydrate errored (not just missing): never persist
_sweep_cursor: Optional[int] = None  # next historical year the sweep visits

# REFRESH_TBA force flag (design §2.3 "manual force"): set per-run by the
# reset endpoints' refresh_tba query param (src/data/main.py scopes it).
_force_refresh: bool = False

_YEAR_RE = re.compile(r"^\d{4}$")
_YEAR_PREFIX_RE = re.compile(r"^(\d{4})")


def reset_state() -> None:
    """Clear all process-local cache state (tests)."""
    global _sweep_cursor
    _manifest.clear()
    _hydrated.clear()
    _dirty.clear()
    _meta_dirty.clear()
    _meta_persisted.clear()
    _blocked.clear()
    _sweep_cursor = None
    set_force_refresh(False)


def set_force_refresh(on: bool) -> None:
    global _force_refresh
    _force_refresh = on


def force_refresh() -> bool:
    """True when every get_tba call must bypass cache and etags entirely:
    the per-run flag (refresh_tba query param on the reset endpoints) or
    REFRESH_TBA=1 in the process environment (covers job drivers that call
    process_year directly, e.g. the reprocess-year Cloud Run job)."""
    return _force_refresh or os.getenv("REFRESH_TBA", "") == "1"


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


def _parse_iso(ts: str) -> Optional[datetime]:
    try:
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


"""
MANIFEST (design §2.4, §3.3)
"""


def stored_etag(url: str) -> Optional[str]:
    entry = _manifest.get(url)
    return entry.get("etag") if entry is not None else None


def validated_within(url: str, hours: float) -> bool:
    """True when the manifest holds an entry for url whose last_validated is
    within the last `hours` hours. Missing entry or unreadable timestamp
    counts as NOT validated (revalidation is the safe direction)."""
    entry = _manifest.get(url)
    if entry is None:
        return False
    ts = _parse_iso(entry.get("last_validated", ""))
    if ts is None:
        return False
    return datetime.now(timezone.utc) - ts < timedelta(hours=hours)


def needs_revalidation(url: str, hours: float) -> bool:
    """Daily-tier candidate check (design §2.3): True only when the manifest
    HAS an entry for url and it has not been validated within `hours`. Paths
    with no manifest state return False — the daily tier never adds requests
    for paths it holds no etag state for (a cold manifest adds nothing;
    populating it is the reset path's job)."""
    return url in _manifest and not validated_within(url, hours)


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


def manifest_paths(archive: str) -> List[str]:
    """Sorted cache-key URLs the manifest tracks for one archive (the daily
    sweep's work list for a year)."""
    return sorted(url for url in _manifest if archive_for(url) == archive)


def sweep_cursor() -> Optional[int]:
    """The next historical year the daily sweep should visit, or None when
    no cursor has ever been stored (first run: start at the oldest year)."""
    return _sweep_cursor


def set_sweep_cursor(year: int) -> None:
    """Advance the sweep cursor. Content-dirties the global archive so the
    next persist() always uploads it — the cursor must survive process
    restarts or the round-robin would revisit the same year."""
    global _sweep_cursor
    _sweep_cursor = year
    _dirty.add(GLOBAL_ARCHIVE)


def _hydrate_sweep_cursor(entry: Dict[str, str]) -> None:
    global _sweep_cursor
    if _sweep_cursor is not None:
        return  # a cursor set this process is fresher than the archive's
    try:
        _sweep_cursor = int(entry.get("next_year", ""))
    except (TypeError, ValueError):
        pass


def record_not_modified(url: str, etag: Optional[str]) -> None:
    """A 304 confirmed the request etag. Refresh last_validated only when
    that etag is the one we have stored (the pickle's etag) — a 304 against
    a caller-supplied foreign etag says nothing about our pickle."""
    entry = _manifest.get(url)
    if etag is None or entry is None or entry.get("etag") != etag:
        return
    entry["last_validated"] = _now_iso()
    _meta_dirty.add(archive_for(url))


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
    if name in _meta_persisted:
        manifest[ARCHIVE_META_KEY] = {"meta_persisted": _meta_persisted[name]}
    if name == GLOBAL_ARCHIVE and _sweep_cursor is not None:
        manifest[SWEEP_CURSOR_KEY] = {"next_year": str(_sweep_cursor)}
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
    Only regular-file members whose resolved path stays inside dest are
    written — absolute-path and ``..``-escape members are skipped, and
    non-file members (symlinks, devices, dirs) are never created."""
    manifest: Dict[str, Dict[str, str]] = {}
    root = os.path.normpath(dest)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            # os.path.join discards root for absolute member names, so the
            # containment check below rejects those too.
            target = os.path.normpath(os.path.join(root, member.name))
            if not target.startswith(root + os.sep):
                continue
            handle = tar.extractfile(member)
            if handle is None:
                continue
            payload = handle.read()
            if target == os.path.join(root, MANIFEST_NAME):
                manifest = json.loads(payload)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "wb") as out:
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
                meta = manifest.pop(ARCHIVE_META_KEY, None)
                if meta is not None and "meta_persisted" in meta:
                    _meta_persisted.setdefault(name, meta["meta_persisted"])
                cursor = manifest.pop(SWEEP_CURSOR_KEY, None)
                if cursor is not None and name == GLOBAL_ARCHIVE:
                    _hydrate_sweep_cursor(cursor)
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


def _meta_persist_due(name: str) -> bool:
    stamp = _parse_iso(_meta_persisted.get(name, ""))
    if stamp is None:
        return True
    return datetime.now(timezone.utc) - stamp >= timedelta(
        hours=META_PERSIST_INTERVAL_HOURS
    )


def persist() -> None:
    """Upload every dirty archive (re-packed from the local cache dir).
    Dirty-only, atomic per archive, never raises; a failed upload stays
    dirty for a later retry. Content-dirty archives always upload;
    metadata-only dirtiness (304s refreshing last_validated) uploads at most
    once per META_PERSIST_INTERVAL_HOURS, tracked by a stamp stored in the
    archive's own manifest so the debounce survives process restarts."""
    if _gcs_disabled():
        return
    for name in sorted(_dirty | _meta_dirty):
        if name in _blocked:
            print(
                f"WARNING: not persisting TBA cache {name}: "
                "its hydrate failed this process"
            )
            continue
        if name not in _dirty and not _meta_persist_due(name):
            continue  # debounced: stays meta-dirty for a later cycle
        prev_stamp = _meta_persisted.get(name)
        # Stamp before packing so the archive's manifest carries it.
        _meta_persisted[name] = _now_iso()
        try:
            _upload_archive(name, pack_archive(name, _cache_dir()))
            print(f"TBA cache: persisted {name}")
        except Exception:
            # Roll the stamp back so the retry is not debounced for hours.
            if prev_stamp is None:
                _meta_persisted.pop(name, None)
            else:
                _meta_persisted[name] = prev_stamp
            print(f"WARNING: failed to persist TBA cache archive {name}")
            traceback.print_exc()
            continue
        _dirty.discard(name)
        _meta_dirty.discard(name)


__all__: List[str] = [
    "archive_for",
    "extract_archive",
    "force_refresh",
    "hydrate",
    "manifest_paths",
    "needs_revalidation",
    "pack_archive",
    "persist",
    "record_not_modified",
    "record_success",
    "reset_state",
    "set_force_refresh",
    "set_sweep_cursor",
    "stored_etag",
    "sweep_cursor",
    "validated_within",
]
