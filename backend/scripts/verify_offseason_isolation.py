"""Prove the offseason sandbox changes no season rating.

Loads each year's snapshot, runs the full EPA pass twice -- once with the fork
suppressed (frozen, the pre-change behavior) and once with it live -- then
diffs every TeamYear rating field. Any difference is a containment bug.

This covers both spec invariants:
  - within-season: a 2025 offseason match must not move a 2025 season rating
  - cross-season: it reaches a later year only via all_team_years ->
    get_init_epa, and that path starts from these very fields

Usage:
    cd backend && PROD=True poetry run python scripts/verify_offseason_isolation.py 2025 2026
"""

import sys
from typing import Any, Dict, List

from src.data.epa.agg import process_year as process_year_agg
from src.data.epa.calc import process_year as process_year_calc
from src.google.snapshot import read_snapshot
from src.models.backtest import SandboxSwitch, install_switch

FIELDS = [
    "epa",
    "epa_start",
    "epa_pre_champs",
    "epa_max",
    "unitless_epa",
    "norm_epa",
    "auto_epa",
    "teleop_epa",
    "endgame_epa",
    "rp_1_epa",
    "rp_2_epa",
    "rp_3_epa",
    "tiebreaker_epa",
    "total_epa_rank",
    "total_epa_percentile",
    "country_epa_rank",
    "state_epa_rank",
    "district_epa_rank",
] + [f"comp_{i}_epa" for i in range(10)]


def snapshot_team_years(year: int, sandbox: bool) -> Dict[str, Dict[str, Any]]:
    loaded = read_snapshot(year)
    if loaded is None:
        raise SystemExit(f"no snapshot for {year}")
    objs, _ = loaded

    SandboxSwitch.enabled = sandbox
    try:
        objs = process_year_calc(objs, {})
        objs = process_year_agg(objs)
    finally:
        SandboxSwitch.enabled = True

    return {key: {f: getattr(ty, f, None) for f in FIELDS} for key, ty in objs[1].items()}


def main(years: List[int]) -> int:
    install_switch()
    failures = 0
    for year in years:
        frozen = snapshot_team_years(year, sandbox=False)
        live = snapshot_team_years(year, sandbox=True)

        if set(frozen) != set(live):
            print(f"FAIL {year}: team-year key sets diverged")
            failures += 1
            continue

        diffs = []
        for key in frozen:
            for field in FIELDS:
                a, b = frozen[key][field], live[key][field]
                if a != b:
                    diffs.append(f"{key}.{field}: {a!r} != {b!r}")

        if diffs:
            failures += 1
            print(f"FAIL {year}: {len(diffs)} differing TeamYear fields")
            for line in diffs[:20]:
                print(f"  {line}")
        else:
            print(f"OK   {year}: {len(frozen)} team-years bit-identical")

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main([int(a) for a in sys.argv[1:]] or [2025, 2026]))
