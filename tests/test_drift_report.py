from __future__ import annotations

import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from core.backtest_engine import BacktestResult
from core.drift_report import build_drift_summary, write_drift_report
from core.trade_log import TRADE_LOG_FIELDS
from scheduler.run_drift_report import generate_drift_report
from core.config import load_config


class DriftReportTests(unittest.TestCase):
    def test_build_drift_summary_compares_paper_rows_to_backtest(self):
        result = BacktestResult(
            starting_fund=10000.0,
            ending_equity=10100.0,
            total_return_pct=1.0,
            sharpe=0.0,
            max_drawdown_pct=0.0,
            win_rate=50.0,
            trade_count=1,
            closed_trade_count=1,
            metrics={"avg_hold_days": 2.0},
            attribution={
                "by_strategy": {
                    "iron_condor": {
                        "trade_count": 1,
                        "total_entry_slippage": 4.0,
                    }
                }
            },
        )
        with TemporaryDirectory() as tmp:
            trade_log_path = Path(tmp) / "trades.csv"
            with trade_log_path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=TRADE_LOG_FIELDS)
                writer.writeheader()
                writer.writerow(
                    {
                        "timestamp": "2026-04-20T10:00:00+00:00",
                        "mode": "paper",
                        "strategy": "iron_condor",
                        "underlying": "SPY",
                        "strategy_id": "iron-SPY-1",
                        "status": "open",
                        "expected_entry_price": "1.00",
                        "actual_entry_price": "0.95",
                        "leg_slippage_dollars": "5.00",
                        "order_duration_seconds": "2.5",
                    }
                )
                writer.writerow(
                    {
                        "timestamp": "2026-04-23T10:00:00+00:00",
                        "mode": "paper",
                        "strategy": "iron_condor",
                        "underlying": "SPY",
                        "strategy_id": "iron-SPY-1",
                        "status": "closed",
                        "exit_reason": "take_profit",
                        "pnl_pct": "10",
                    }
                )

            summary = build_drift_summary(trade_log_path=trade_log_path, backtest_result=result)

        self.assertEqual(summary["paper"]["closed_trade_count"], 1)
        self.assertEqual(summary["paper"]["avg_entry_price_drift"], -0.05)
        self.assertEqual(summary["paper"]["avg_hold_days"], 3.0)
        self.assertEqual(summary["paper"]["exit_reasons"], {"take_profit": 1})
        self.assertEqual(summary["backtest"]["pnl_dollars"], 100.0)
        self.assertEqual(summary["drift"]["total_slippage_delta"], 1.0)
        self.assertEqual(summary["drift"]["avg_hold_days_delta"], 1.0)
        self.assertIn("limitations", summary)

    def test_write_drift_report_persists_markdown_with_json_payload(self):
        summary = {
            "paper": {
                "strategy_count": 0,
                "closed_trade_count": 0,
                "avg_expected_entry_price": None,
                "avg_actual_entry_price": None,
                "avg_entry_price_drift": None,
                "total_leg_slippage_dollars": 0.0,
                "avg_leg_slippage_dollars": None,
                "avg_order_duration_seconds": None,
                "avg_hold_days": None,
                "avg_pnl_pct": None,
                "exit_reasons": {},
            },
            "backtest": {
                "trade_count": 0,
                "closed_trade_count": 0,
                "total_return_pct": 0.0,
                "pnl_dollars": 0.0,
                "avg_hold_days": 0.0,
                "total_entry_slippage": 0.0,
                "avg_entry_slippage": 0.0,
                "exit_model": "model",
                "rejected_reasons": {},
            },
            "drift": {
                "closed_trade_count_delta": 0,
                "avg_entry_price_drift": None,
                "avg_hold_days_delta": None,
                "total_slippage_delta": 0.0,
                "avg_slippage_delta": None,
                "pnl_dollars_delta": None,
                "pnl_pct_delta": None,
            },
            "limitations": ["limited paper rows"],
        }
        with TemporaryDirectory() as tmp:
            path = write_drift_report(summary, reports_dir=Path(tmp), days=30)
            text = path.read_text(encoding="utf-8")

        self.assertEqual(path.name, "paper_vs_backtest_30d.md")
        self.assertIn("# HawksOptions Paper vs Backtest Drift", text)
        self.assertIn("limited paper rows", text)
        self.assertIn("```json", text)

    def test_generate_drift_report_runs_backtest_and_writes_report(self):
        config = load_config()
        config["account"]["max_single_position_risk_pct"] = 1.0
        config["account"]["max_portfolio_risk_pct"] = 1.0
        with TemporaryDirectory() as tmp:
            config["reporting"]["reports_dir"] = tmp
            config["reporting"]["trade_log_file"] = str(Path(tmp) / "trades.csv")
            summary, path = generate_drift_report(config=config, days=10, starting_fund=10000.0)
            self.assertTrue(path.exists())

        self.assertIn("paper", summary)
        self.assertIn("backtest", summary)
        self.assertIn("drift", summary)


if __name__ == "__main__":
    unittest.main()
