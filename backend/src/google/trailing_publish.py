"""Trailing publish (trailing-year refresh spec §6).

While the trailing window is open the trailing year publishes like the
current year: its site blobs and its Parquet go out as versioned ``v2/``
objects through the manifest, in ONE manifest write, and no hist blob is
written. The write re-checks the window inside the manifest mutation, so a
trailing publish that commits after the window closed aborts (Review Focus
4) rather than re-adding keys window close has just removed.
"""

import json
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from src.data import trailing
from src.data.utils import objs_type
from src.db.models import Team
from src.google import storage
from src.google.parquet import build_parquet_uploads, parquet_logical
from src.google.publish import Manifest, plan_uploads
from src.google.year_path import year_from_path

TRAILING_STATUS_OBJECT = "trailing/status.json"


class TrailingClosed(Exception):
    """The trailing window closed before the trailing publish committed."""


# Logical site-blob paths rendered by the last trailing publish, per year.
# stamp_trailing_bootstrap removes the year's other year keys.
_last_rendered: Dict[int, Set[str]] = {}


def _require_live(year: int) -> None:
    if not trailing.is_live_year(year):
        raise TrailingClosed(f"trailing window closed for {year}")


def trailing_publish(year: int, objs: objs_type, teams: List[Team]) -> bool:
    """Publish the trailing year's site blobs and Parquet in one manifest
    write; unchanged paths upload nothing. Returns whether
    ``parquet/{year}/team_years.parquet`` changed. Raises TrailingClosed
    (writing no manifest) once the window has closed, and lets a
    ManifestReadError propagate before any upload."""
    _require_live(year)
    rendered = storage.render_site_blobs(year, objs, teams)
    parquet = build_parquet_uploads(year, objs, teams)

    prev = storage.read_manifest()
    cycle = datetime.now(timezone.utc).isoformat()
    plan = plan_uploads(rendered, prev, cycle)
    storage._fold_parquet(plan, prev, parquet)
    team_years_changed = parquet_logical(year, "team_years") in plan.blob_updates

    if plan.uploads or plan.blob_updates or plan.blob_removals:
        # The plan's delta is applied only to the in-memory copy; the guard
        # raising aborts the whole write.
        storage._publish(plan, mutate=lambda m: _require_live(year), legacy=False)
    else:
        _require_live(year)

    _last_rendered[year] = set(rendered)
    return team_years_changed


def _unrendered_year_keys(manifest: Manifest, year: int, keep: Set[str]) -> List[str]:
    return [
        k
        for k in manifest.blobs
        if year_from_path(k) == year
        and not k.startswith("parquet/")
        and k not in keep
    ]


def stamp_trailing_bootstrap(year: int) -> None:
    """After a full trailing cycle's snapshot: remove the year's year keys
    the last trailing publish did not render (Parquet keys stay) and stamp
    ``trailing_year = year``, in one manifest write."""
    keep = _last_rendered.get(year)
    if keep is None:
        raise RuntimeError(f"no trailing publish of {year} in this process")

    def mutate(m: Manifest) -> None:
        _require_live(year)
        for key in _unrendered_year_keys(m, year, keep):
            m.blobs.pop(key, None)
        m.trailing_year = year

    storage.update_manifest(mutate)


def write_trailing_status(status: Dict[str, Any]) -> None:
    """Best-effort ``trailing/status.json`` (no-store), read by /info."""
    try:
        blob = storage._bucket().blob(TRAILING_STATUS_OBJECT)
        blob.cache_control = "no-store"
        blob.upload_from_string(json.dumps(status), "application/json")
    except Exception:
        traceback.print_exc()
        print("WARNING: trailing/status.json write failed")


def read_trailing_status() -> Optional[Dict[str, Any]]:
    """``trailing/status.json`` for /info; best-effort, None on any error
    (including absent or unparseable)."""
    try:
        raw = storage._bucket().blob(TRAILING_STATUS_OBJECT).download_as_bytes()
        status = json.loads(raw)
        return status if isinstance(status, dict) else None
    except Exception:
        return None


def team_years_digest(year: int) -> Optional[str]:
    """The manifest digest of ``parquet/{year}/team_years.parquet``."""
    manifest = storage.read_manifest()
    if manifest is None:
        return None
    return manifest.hash_for(parquet_logical(year, "team_years"))
