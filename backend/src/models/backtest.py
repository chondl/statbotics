"""Offseason sandbox EPA backtest.

Replays a season's matches through the real EPA model twice -- once with the
per-event offseason sandbox active and once with it suppressed -- and reports
prediction quality for offseason matches only.

The gate this exists to serve: the sandbox must beat the frozen baseline on the
real 18-dimensional model before the feature ships. The scalar-offset proxy
recorded in the design spec justified building this; it does not justify
merging.

Usage:
    cd backend && PROD=True poetry run python -m src.models.backtest 2025 2026
    cd backend && PROD=True poetry run python -m src.models.backtest 2026 --sweep 0 6 12
"""

import argparse
import math
from collections import defaultdict
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from src.data.epa.calc import process_year as process_year_calc
from src.db.models import Match
from src.google.snapshot import read_snapshot
from src.models.template import Model
from src.types.enums import EventType, MatchStatus, MatchWinner


@dataclass
class Metrics:
    count: int
    rmse: float
    bias: float
    acc: float
    brier: float


def bucket_of(index: int) -> str:
    """Within-event match index -> reporting bucket."""
    if index < 20:
        return "1-20"
    if index < 50:
        return "21-50"
    return "51+"


_OUTCOME = {MatchWinner.RED: 1.0, MatchWinner.BLUE: 0.0, MatchWinner.TIE: 0.5}


def score_metrics_for(matches: List[Match]) -> Metrics:
    """Alliance-level score error plus win-probability quality.

    bias is actual - predicted, so a negative bias means predictions ran high.
    """
    residuals: List[float] = []
    briers: List[float] = []
    correct = 0
    counted = 0

    for match in matches:
        if match.epa_red_score_pred is None or match.epa_blue_score_pred is None:
            continue
        if match.red_score is None or match.blue_score is None:
            continue
        residuals.append(match.red_score - match.epa_red_score_pred)
        residuals.append(match.blue_score - match.epa_blue_score_pred)

        actual = _OUTCOME.get(match.get_winner())
        if actual is None or match.epa_win_prob is None:
            continue
        counted += 1
        briers.append((match.epa_win_prob - actual) ** 2)
        if (match.epa_win_prob >= 0.5) == (actual >= 0.5) or actual == 0.5:
            correct += 1

    if not residuals:
        return Metrics(0, 0.0, 0.0, 0.0, 0.0)

    rmse = math.sqrt(sum(x * x for x in residuals) / len(residuals))
    bias = sum(residuals) / len(residuals)
    acc = correct / counted if counted else 0.0
    brier = sum(briers) / len(briers) if briers else 0.0
    return Metrics(counted, rmse, bias, acc, brier)


class SandboxSwitch:
    """Lets the harness suppress the fork without editing the model."""

    enabled = True


_original_sandbox = Model._sandbox
_installed = False


def install_switch() -> None:
    """Patch Model._sandbox so `enabled = False` reproduces frozen behavior:
    predictions still recorded, but no rating ever moves."""
    global _installed
    if _installed:
        return

    @contextmanager
    def switched(self, event):
        if not SandboxSwitch.enabled and event.type == EventType.OFFSEASON:
            saved = self.update_team
            self.update_team = lambda *a, **k: None
            try:
                yield True
            finally:
                self.update_team = saved
            return
        with _original_sandbox(self, event) as sandboxed:
            yield sandboxed

    Model._sandbox = switched
    _installed = True


def run_year(year: int, sandbox: bool) -> Tuple[Dict[str, List[Match]], List[str]]:
    """Replay a year through the real model. Returns completed offseason matches
    grouped by event key in chronological order, plus the offseason event keys."""
    loaded = read_snapshot(year)
    if loaded is None:
        raise SystemExit(f"no snapshot for {year}; run the pipeline first")
    objs, _teams = loaded

    SandboxSwitch.enabled = sandbox
    try:
        objs = process_year_calc(objs, {})
    finally:
        SandboxSwitch.enabled = True

    offseason_keys = [e.key for e in objs[2].values() if e.type == EventType.OFFSEASON]
    keyset = set(offseason_keys)
    by_event: Dict[str, List[Match]] = defaultdict(list)
    for match in objs[4].values():
        if match.event in keyset and match.status == MatchStatus.COMPLETED:
            by_event[match.event].append(match)
    for key in by_event:
        by_event[key].sort(key=lambda m: (m.time, m.key))
    return by_event, offseason_keys


def report(label: str, by_event: Dict[str, List[Match]]) -> Dict[str, Metrics]:
    buckets: Dict[str, List[Match]] = defaultdict(list)
    quals: List[Match] = []
    elims: List[Match] = []
    every: List[Match] = []
    for matches in by_event.values():
        for i, match in enumerate(matches):
            buckets[bucket_of(i)].append(match)
            every.append(match)
            (elims if match.elim else quals).append(match)

    out: Dict[str, Metrics] = {
        "ALL": score_metrics_for(every),
        "QUALS": score_metrics_for(quals),
        "ELIMS": score_metrics_for(elims),
    }
    for name in ("1-20", "21-50", "51+"):
        out[name] = score_metrics_for(buckets.get(name, []))

    print(f"  {label}")
    for name, m in out.items():
        if m.count == 0:
            continue
        print(
            f"    {name:>6}  n={m.count:5}  rmse={m.rmse:8.2f}  "
            f"bias={m.bias:+8.2f}  acc={m.acc:.4f}  brier={m.brier:.4f}"
        )
    return out


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Offseason sandbox EPA backtest")
    parser.add_argument("years", nargs="+", type=int)
    parser.add_argument(
        "--sweep",
        nargs="*",
        type=int,
        default=None,
        help="SANDBOX_SEED_COUNT values to try (default: just the shipped value)",
    )
    args = parser.parse_args(argv)

    install_switch()

    import src.models.epa.main as epa_main

    shipped_seed = epa_main.SANDBOX_SEED_COUNT

    for year in args.years:
        print(f"===== {year} =====")
        frozen, keys = run_year(year, sandbox=False)
        print(
            f"  {len(keys)} offseason events, {sum(len(v) for v in frozen.values())} matches"
        )
        base = report("FROZEN (pre-change behavior)", frozen)

        for seed in args.sweep if args.sweep else [shipped_seed]:
            epa_main.SANDBOX_SEED_COUNT = seed
            live, _ = run_year(year, sandbox=True)
            new = report(f"SANDBOX (SANDBOX_SEED_COUNT={seed})", live)
            print(
                f"    -> ALL   acc {new['ALL'].acc - base['ALL'].acc:+.4f}"
                f"   brier {base['ALL'].brier - new['ALL'].brier:+.4f}"
                f"   rmse {base['ALL'].rmse - new['ALL'].rmse:+.2f}"
            )
            if new["ELIMS"].count:
                print(
                    f"    -> ELIMS acc {new['ELIMS'].acc - base['ELIMS'].acc:+.4f}"
                    f"   brier {base['ELIMS'].brier - new['ELIMS'].brier:+.4f}"
                    f"   rmse {base['ELIMS'].rmse - new['ELIMS'].rmse:+.2f}"
                )
        epa_main.SANDBOX_SEED_COUNT = shipped_seed
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
