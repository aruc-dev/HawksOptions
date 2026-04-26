"""Run the deterministic sample-data backtest."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.backtest_engine import run_backtest
from core.config import load_config
from strategies import build_enabled_strategies


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the HawksOptions backtest")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--fund", type=float, default=10000.0)
    parser.add_argument("--start-date", help="optional YYYY-MM-DD replay start date")
    args = parser.parse_args(argv)

    config = load_config()
    strategies = build_enabled_strategies(config)
    result, report_path = run_backtest(
        config=config,
        strategies=strategies,
        days=max(5, args.days),
        starting_fund=max(1000.0, args.fund),
        start_date=date.fromisoformat(args.start_date) if args.start_date else None,
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
                "report_path": str(report_path),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
