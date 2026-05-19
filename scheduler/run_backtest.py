"""Run the deterministic sample-data backtest."""

from __future__ import annotations

import argparse
import json
import sys
from copy import deepcopy
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.backtest_engine import run_backtest
from core.config import load_config
from strategies import build_enabled_strategies


DEFAULT_FIXTURE_FILE = "tests/fixtures/backtest_market_data.json"


def _apply_backtest_source(
    config: dict,
    *,
    source: str | None,
    historical_data_file: str | None,
    fixture_fallback_to_sample: bool,
) -> dict:
    config = deepcopy(config)
    backtest_cfg = config.setdefault("backtest", {})
    if source:
        normalized = source.lower().replace("_", "-")
        if normalized in {"sample", "deterministic"}:
            backtest_cfg["data_source"] = "sample"
        elif normalized in {"fixture", "historical-replay", "json-replay"}:
            backtest_cfg["data_source"] = "historical_replay"
            if normalized == "fixture":
                backtest_cfg["fixture_file"] = DEFAULT_FIXTURE_FILE
                backtest_cfg.pop("historical_data_file", None)
                backtest_cfg.pop("historical_provider", None)
            else:
                backtest_cfg.pop("fixture_file", None)
        elif normalized in {"alpaca-history", "alpaca"}:
            backtest_cfg["data_source"] = "historical_replay"
            backtest_cfg["historical_provider"] = "alpaca"
            backtest_cfg.pop("fixture_file", None)
        else:
            raise SystemExit(f"unsupported backtest source {source!r}")
    if historical_data_file:
        backtest_cfg["historical_data_file"] = historical_data_file
        backtest_cfg.pop("fixture_file", None)
    if fixture_fallback_to_sample:
        backtest_cfg["fixture_fallback_to_sample"] = True
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the HawksOptions backtest")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--fund", type=float, default=10000.0)
    parser.add_argument("--start-date", help="optional YYYY-MM-DD replay start date")
    parser.add_argument("--start", dest="start_date_alias", help="alias for --start-date")
    parser.add_argument("--end", help="optional YYYY-MM-DD replay end date; overrides --days when used with --start")
    parser.add_argument(
        "--source",
        choices=["sample", "fixture", "historical-replay", "alpaca-history"],
        help="market data source for the run",
    )
    parser.add_argument("--historical-data-file", help="normalized JSON/CSV/Parquet replay file")
    parser.add_argument(
        "--fixture-fallback-to-sample",
        action="store_true",
        help="allow historical replay gaps to fall back to deterministic sample data",
    )
    args = parser.parse_args(argv)

    config = _apply_backtest_source(
        load_config(),
        source=args.source,
        historical_data_file=args.historical_data_file,
        fixture_fallback_to_sample=args.fixture_fallback_to_sample,
    )
    strategies = build_enabled_strategies(config)
    start_text = args.start_date or args.start_date_alias
    start_date = date.fromisoformat(start_text) if start_text else None
    days = max(5, args.days)
    if args.end:
        if start_date is None:
            raise SystemExit("--end requires --start or --start-date")
        days = max(1, (date.fromisoformat(args.end) - start_date).days + 1)
    result, report_path = run_backtest(
        config=config,
        strategies=strategies,
        days=days,
        starting_fund=max(1000.0, args.fund),
        start_date=start_date,
    )
    print(
        json.dumps(
            {
                "starting_fund": result.starting_fund,
                "ending_equity": result.ending_equity,
                "total_return_pct": result.total_return_pct,
                "sharpe": result.sharpe,
                "max_drawdown_pct": result.max_drawdown_pct,
                "win_rate": result.win_rate,
                "trade_count": result.trade_count,
                "closed_trade_count": result.closed_trade_count,
                "rejected_reasons": result.rejected_reasons,
                "metrics": result.metrics,
                "attribution": result.attribution,
                "attribution_report_path": result.provenance.get("attribution_report_path", ""),
                "report_path": str(report_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
