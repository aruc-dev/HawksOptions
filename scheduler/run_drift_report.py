"""Generate a paper-trading versus backtest drift report."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.backtest_engine import run_backtest
from core.config import load_config, reporting_path
from core.drift_report import build_drift_summary, write_drift_report
from strategies import build_enabled_strategies


def generate_drift_report(
    *,
    config: dict | None = None,
    days: int = 30,
    starting_fund: float = 10000.0,
    start_date: date | None = None,
) -> tuple[dict, Path]:
    config = config or load_config()
    result, _ = run_backtest(
        config=config,
        strategies=build_enabled_strategies(config),
        days=max(5, days),
        starting_fund=max(1000.0, starting_fund),
        start_date=start_date,
    )
    summary = build_drift_summary(
        trade_log_path=reporting_path(config, "trade_log_file"),
        backtest_result=result,
    )
    report_path = write_drift_report(
        summary,
        reports_dir=reporting_path(config, "reports_dir"),
        days=max(5, days),
    )
    return summary, report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate paper-vs-backtest drift report")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--fund", type=float, default=10000.0)
    parser.add_argument("--start-date", help="optional YYYY-MM-DD replay start date")
    args = parser.parse_args(argv)
    summary, report_path = generate_drift_report(
        days=args.days,
        starting_fund=args.fund,
        start_date=date.fromisoformat(args.start_date) if args.start_date else None,
    )
    print(json.dumps({"report_path": str(report_path), "summary": summary}, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
