"""Year-from-path grammar and the per-year hist-epoch rule (trailing-year
refresh spec §8).

Stdlib only, with no ``src.*`` imports: an ops verify script loads this file
by path (importlib.util.spec_from_file_location) so the rule has exactly one
backend copy. The frontend mirrors it; both sides check the shared vectors in
``backend/tests/data/year_from_path_vectors.json``.
"""

import re
from typing import Any, Mapping, Optional

_YEAR_PATTERNS = [
    # event/{key}: the key's leading four digits.
    re.compile(r"^event/(\d{4})"),
    # events/Y…, team_years/Y…, noteworthy_matches/Y… (e.g. team_years/2026.limit=…)
    re.compile(r"^(?:events|team_years|noteworthy_matches)/(\d{4})(?!\d)"),
    # parquet/Y/…
    re.compile(r"^parquet/(\d{4})/"),
    # team/{num}/Y — exactly three segments, Y four digits.
    re.compile(r"^team/[^/]+/(\d{4})$"),
]


def year_from_path(path: str) -> Optional[int]:
    """The season a logical blob path belongs to, or None for paths with no
    year (team/{num}, teams/all, events/all, upcoming_matches…, …)."""
    for pattern in _YEAR_PATTERNS:
        match = pattern.match(path)
        if match:
            return int(match.group(1))
    return None


def _field(manifest: Any, name: str, default: Any) -> Any:
    if isinstance(manifest, Mapping):
        value = manifest.get(name, default)
    else:
        value = getattr(manifest, name, default)
    return default if value is None else value


def effective_hist_epoch(manifest: Any, year: Optional[int]) -> int:
    """Epoch whose ``hist/{epoch}/…`` objects serve ``year``:
    ``max(hist_epochs[year] ?? 0, hist_epoch)``. A path with no year resolves
    under the global ``hist_epoch``. ``manifest`` is a Manifest or its parsed
    JSON dict."""
    global_epoch = int(_field(manifest, "hist_epoch", 1))
    if year is None:
        return global_epoch
    epochs = _field(manifest, "hist_epochs", {})
    return max(int(epochs.get(str(year), 0)), global_epoch)
