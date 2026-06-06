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
from core.file_lock import atomic_write_text
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
        "metrics": result.metrics,
    }


def _result_metrics(run: dict[str, Any]) -> dict[str, Any]:
    if isinstance(run.get("test"), dict):
        return run["test"]
    return run


def _rank_runs(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ranked = []
    for run in runs:
        metrics = _result_metrics(run)
        ranked.append(
            {
                "label": str(run.get("label") or metrics.get("label") or "run"),
                "return_pct": float(metrics.get("return_pct", 0.0)),
                "sharpe": float(metrics.get("sharpe", 0.0)),
                "drawdown_pct": float(metrics.get("drawdown_pct", 0.0)),
                "win_rate": float(metrics.get("win_rate", 0.0)),
                "trades": int(metrics.get("trades", 0)),
                "closed": int(metrics.get("closed", 0)),
                "ending_equity": float(metrics.get("ending_equity", 0.0)),
                "overrides": metrics.get("overrides", []),
            }
        )
    ranked.sort(
        key=lambda item: (
            item["return_pct"],
            item["sharpe"],
            -item["drawdown_pct"],
            item["win_rate"],
            item["closed"],
        ),
        reverse=True,
    )
    return ranked


def _profitable_runs(
    ranked: list[dict[str, Any]],
    *,
    min_return_pct: float,
    min_trades: int,
) -> list[dict[str, Any]]:
    return [
        item
        for item in ranked
        if item["return_pct"] >= min_return_pct and item["closed"] >= min_trades
    ]


def _reports_dir(config: dict[str, Any]) -> Path:
    configured = str(config.get("reporting", {}).get("reports_dir", "reports"))
    path = Path(configured)
    if path.is_absolute():
        return path
    return BASE_DIR / path


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


def _write_json_tuning_report(*, runs: list[dict[str, Any]], output_dir: Path, top: int) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    path = output_dir / f"tuning_{now:%Y%m%d-%H%M%S}.json"
    payload = {
        "generated_at": now.isoformat(timespec="seconds"),
        "objective": "maximize sharpe + profit factor + expectancy with max drawdown <= 8%",
        "top": _top_runs(runs, limit=top),
        "run_count": len(runs),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    return path


def _write_markdown_tuning_report(
    *,
    config: dict[str, Any],
    summary: dict[str, Any],
    top: int,
) -> Path:
    report_dir = _reports_dir(config) / "tuning"
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    path = report_dir / f"tuning_{timestamp:%Y%m%d_%H%M%S%f}Z.md"
    ranked = list(summary.get("ranked_runs", []))
    profitable = list(summary.get("profitable_runs", []))
    lines = [
        "# HawksOptions Tuning Report",
        "",
        f"- Generated at: {timestamp.isoformat()}",
        f"- Runs: {len(ranked)}",
        f"- Profitable runs: {len(profitable)}",
        "",
        "## Ranked Runs",
        "",
    ]
    if ranked:
        lines.append("| Rank | Label | Return % | Sharpe | Max DD % | Win % | Closed |")
        lines.append("|---:|---|---:|---:|---:|---:|---:|")
        for index, item in enumerate(ranked[:top], start=1):
            lines.append(
                f"| {index} | {item['label']} | {item['return_pct']:.2f} | "
                f"{item['sharpe']:.2f} | {item['drawdown_pct']:.2f} | "
                f"{item['win_rate']:.2f} | {item['closed']} |"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Profitable Candidates", ""])
    if profitable:
        for item in profitable[:top]:
            overrides = ", ".join(
                f"{override['path']}={override['value']}"
                for override in item.get("overrides", [])
                if isinstance(override, dict)
            )
            lines.append(
                f"- {item['label']}: return {item['return_pct']:.2f}%, "
                f"drawdown {item['drawdown_pct']:.2f}%, closed {item['closed']}; "
                f"{overrides or 'no overrides'}"
            )
    else:
        lines.append("- none met the configured profitability and trade-count filters")
    lines.extend(["", "```json", json.dumps(summary, indent=2, sort_keys=True, default=str), "```", ""])
    atomic_write_text(path, "\n".join(lines), lock=False)
    return path


def _write_tuning_report(*, runs: list[dict[str, Any]], output_dir: Path, top: int) -> Path:
    return _write_json_tuning_report(runs=runs, output_dir=output_dir, top=top)


def _write_report(
    *,
    config: dict[str, Any],
    summary: dict[str, Any],
    top: int,
) -> Path:
    return _write_markdown_tuning_report(config=config, summary=summary, top=top)


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
    parser.add_argument("--start-date", help="optional YYYY-MM-DD replay start date")
    parser.add_argument(
        "--walk-forward",
        action="store_true",
        help="split the requested days into train/test windows and report both",
    )
    parser.add_argument("--label", default="run", help="label printed in the summary")
    parser.add_argument("--report", action="store_true", help="write a ranked tuning report under reports/tuning")
    parser.add_argument("--top", type=int, default=10, help="number of ranked rows to include in reports")
    parser.add_argument("--min-trades", type=int, default=1, help="minimum closed trades for profitable candidates")
    parser.add_argument("--min-return-pct", type=float, default=0.0, help="minimum return percent for profitable candidates")
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
    ranked_runs = _rank_runs(runs)
    profitable = _profitable_runs(
        ranked_runs,
        min_return_pct=args.min_return_pct,
        min_trades=args.min_trades,
    )
    if len(runs) == 1 and not args.walk_forward:
        summary = {**runs[0], "ranked_runs": ranked_runs, "profitable_runs": profitable}
    else:
        summary = {"runs": runs, "ranked_runs": ranked_runs, "profitable_runs": profitable}
    if grid_options:
        output_path = _write_json_tuning_report(runs=runs, output_dir=Path(args.output_dir), top=max(1, args.top))
        summary["tuning_report_path"] = str(output_path)
        summary["top"] = _top_runs(runs, limit=max(1, args.top))
    if args.report:
        summary["report_path"] = str(_write_report(config=base_config, summary=summary, top=max(1, args.top)))
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
