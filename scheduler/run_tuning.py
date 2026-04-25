"""Tuning harness — run backtests with config overrides and emit a one-line summary.

Used to iterate strategy parameters quickly without editing config.yaml between
runs. Overrides are applied via dotted paths, e.g.::

    python3 scheduler/run_tuning.py \
        --override strategies.iron_condor.profit_take_pct=0.25 \
        --override gates.min_dte_entry=14

The base config and underlyings are loaded from disk, deep-copied, then
patched in-memory. Nothing on disk is modified.
"""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.backtest_engine import run_backtest
from core.config import load_config
from strategies import build_enabled_strategies


def _coerce(value: str) -> Any:
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if value.lower() in {"null", "none"}:
        return None
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def _apply_override(config: dict[str, Any], path: str, value: Any) -> None:
    parts = path.split(".")
    cursor: Any = config
    for part in parts[:-1]:
        if not isinstance(cursor, dict) or part not in cursor or not isinstance(cursor[part], dict):
            raise KeyError(f"override path {path!r} does not resolve")
        cursor = cursor[part]
    if not isinstance(cursor, dict):
        raise KeyError(f"override path {path!r} does not resolve")
    cursor[parts[-1]] = value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Tuning-loop backtest harness")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--fund", type=float, default=10000.0)
    parser.add_argument(
        "--override",
        action="append",
        default=[],
        help="dotted-path override, e.g. strategies.iron_condor.profit_take_pct=0.25",
    )
    parser.add_argument("--label", default="run", help="label printed in the summary")
    args = parser.parse_args(argv)

    config = deepcopy(load_config())
    overrides_applied: list[tuple[str, Any]] = []
    for raw in args.override:
        if "=" not in raw:
            raise SystemExit(f"override {raw!r} must be path=value")
        path, _, raw_value = raw.partition("=")
        value = _coerce(raw_value)
        _apply_override(config, path, value)
        overrides_applied.append((path, value))

    strategies = build_enabled_strategies(config)
    result, _ = run_backtest(
        config=config,
        strategies=strategies,
        days=max(5, args.days),
        starting_fund=max(1000.0, args.fund),
    )
    summary = {
        "label": args.label,
        "overrides": [{"path": path, "value": value} for path, value in overrides_applied],
        "return_pct": result.total_return_pct,
        "sharpe": result.sharpe,
        "drawdown_pct": result.max_drawdown_pct,
        "win_rate": result.win_rate,
        "trades": result.trade_count,
        "closed": result.closed_trade_count,
        "ending_equity": result.ending_equity,
    }
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
