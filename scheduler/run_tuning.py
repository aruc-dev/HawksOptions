"""Tuning harness: run backtests with config overrides and emit a one-line summary.

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
from datetime import date, datetime, timedelta, timezone
from itertools import product
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.backtest_engine import run_backtest
from core.config import load_config
from strategies import build_enabled_strategies


VERTICAL_SPREAD_GRID = [
    "strategies.vertical_spread.short_delta=-0.20,-0.25,-0.30",
    "strategies.vertical_spread.long_delta=-0.08,-0.10,-0.12",
    "strategies.vertical_spread.target_dte=28,35,42",
    "strategies.vertical_spread.profit_take_pct=0.35,0.50,0.65",
    "strategies.vertical_spread.loss_stop_multiple=1.0,1.5,2.0",
]


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


def _parse_assignment(raw: str) -> tuple[str, Any]:
    if "=" not in raw:
        raise SystemExit(f"override {raw!r} must be path=value")
    path, _, raw_value = raw.partition("=")
    return path, _coerce(raw_value)


def _parse_grid(raw_items: list[str]) -> list[list[tuple[str, Any]]]:
    grids: list[list[tuple[str, Any]]] = []
    for raw in raw_items:
        if "=" not in raw:
            raise SystemExit(f"grid {raw!r} must be path=v1,v2")
        path, _, values = raw.partition("=")
        options = [(path, _coerce(value.strip())) for value in values.split(",") if value.strip()]
        if not options:
            raise SystemExit(f"grid {raw!r} has no values")
        grids.append(options)
    return grids


def _run_once(
    *,
    base_config: dict[str, Any],
    overrides: list[tuple[str, Any]],
    label: str,
    days: int,
    fund: float,
    start_date: date | None,
) -> dict[str, Any]:
    config = deepcopy(base_config)
    for path, value in overrides:
        _apply_override(config, path, value)
    strategies = build_enabled_strategies(config)
    result, _ = run_backtest(
        config=config,
        strategies=strategies,
        days=max(1, days),
        starting_fund=max(1000.0, fund),
        start_date=start_date,
    )
    return {
        "label": label,
        "overrides": [{"path": path, "value": value} for path, value in overrides],
        "return_pct": result.total_return_pct,
        "sharpe": result.sharpe,
        "drawdown_pct": result.max_drawdown_pct,
        "profit_factor": result.metrics.get("profit_factor", 0.0),
        "expectancy": result.metrics.get("expectancy", 0.0),
        "win_rate": result.win_rate,
        "trades": result.trade_count,
        "closed": result.closed_trade_count,
        "rejected_reasons": result.rejected_reasons,
        "ending_equity": result.ending_equity,
    }


def _score_run(run: dict[str, Any]) -> float:
    metrics = run.get("test", run)
    sharpe = float(metrics.get("sharpe", 0.0))
    profit_factor = float(metrics.get("profit_factor", 0.0))
    expectancy = float(metrics.get("expectancy", 0.0))
    drawdown = float(metrics.get("drawdown_pct", 0.0))
    penalty = max(0.0, drawdown - 8.0) * 2.0
    return round(sharpe + profit_factor + (expectancy / 100.0) - penalty, 6)


def _top_runs(runs: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    eligible = []
    for run in runs:
        metrics = run.get("test", run)
        enriched = deepcopy(run)
        enriched["score"] = _score_run(run)
        enriched["passes_constraints"] = (
            float(metrics.get("drawdown_pct", 0.0)) <= 8.0
            and float(metrics.get("profit_factor", 0.0)) >= 1.2
            and float(metrics.get("expectancy", 0.0)) > 0.0
        )
        eligible.append(enriched)
    return sorted(eligible, key=lambda item: (item["passes_constraints"], item["score"]), reverse=True)[:limit]


def _write_tuning_report(*, runs: list[dict[str, Any]], output_dir: Path, top: int) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"tuning_{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "objective": "maximize sharpe + profit factor + expectancy with max drawdown <= 8%",
        "top": _top_runs(runs, limit=top),
        "run_count": len(runs),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path


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
    parser.add_argument(
        "--grid",
        action="append",
        default=[],
        help="grid override, e.g. strategies.iron_condor.profit_take_pct=0.25,0.35",
    )
    parser.add_argument(
        "--preset",
        choices=["vertical-spread"],
        help="append a production-readiness grid preset",
    )
    parser.add_argument("--output-dir", default="reports/tuning", help="directory for top-run tuning reports")
    parser.add_argument("--top", type=int, default=5, help="number of top parameter sets to persist")
    parser.add_argument("--start-date", help="optional YYYY-MM-DD replay start date")
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="split the requested days into train/test windows and report both",
    )
    parser.add_argument("--label", default="run", help="label printed in the summary")
    args = parser.parse_args(argv)

    base_config = deepcopy(load_config())
    fixed_overrides = [_parse_assignment(raw) for raw in args.override]
    grid_items = list(args.grid)
    if args.preset == "vertical-spread":
        grid_items.extend(VERTICAL_SPREAD_GRID)
    grid_options = _parse_grid(grid_items)
    combinations = list(product(*grid_options)) if grid_options else [()]
    start = date.fromisoformat(args.start_date) if args.start_date else None
    runs = []
    for index, combo in enumerate(combinations, start=1):
        overrides = fixed_overrides + list(combo)
        label = args.label if len(combinations) == 1 else f"{args.label}-{index}"
        if args.walk_forward:
            total_days = max(5, args.days)
            train_days = max(1, total_days // 2)
            test_days = total_days - train_days
            train_start = start
            test_start = (start + timedelta(days=train_days)) if start else None
            runs.append(
                {
                    "label": label,
                    "train": _run_once(
                        base_config=base_config,
                        overrides=overrides,
                        label=f"{label}-train",
                        days=train_days,
                        fund=args.fund,
                        start_date=train_start,
                    ),
                    "test": _run_once(
                        base_config=base_config,
                        overrides=overrides,
                        label=f"{label}-test",
                        days=test_days,
                        fund=args.fund,
                        start_date=test_start,
                    ),
                }
            )
        else:
            runs.append(
                _run_once(
                    base_config=base_config,
                    overrides=overrides,
                    label=label,
                    days=max(5, args.days),
                    fund=args.fund,
                    start_date=start,
                )
            )
    summary = runs[0] if len(runs) == 1 and not args.walk_forward else {"runs": runs}
    if grid_options:
        output_path = _write_tuning_report(runs=runs, output_dir=Path(args.output_dir), top=max(1, args.top))
        if isinstance(summary, dict):
            summary["tuning_report_path"] = str(output_path)
            summary["top"] = _top_runs(runs, limit=max(1, args.top))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
