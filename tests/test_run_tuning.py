from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from core.backtest_engine import BacktestResult
from scheduler import run_tuning


class RunTuningTests(unittest.TestCase):
    def test_walk_forward_split_does_not_exceed_requested_days(self):
        seen_days = []

        def fake_run_backtest(**kwargs):
            seen_days.append(kwargs["days"])
            return (
                BacktestResult(
                    starting_fund=kwargs["starting_fund"],
                    ending_equity=kwargs["starting_fund"],
                    total_return_pct=0.0,
                    sharpe=0.0,
                    max_drawdown_pct=0.0,
                    win_rate=0.0,
                    trade_count=0,
                    closed_trade_count=0,
                ),
                None,
            )

        with patch.object(run_tuning, "run_backtest", side_effect=fake_run_backtest):
            with redirect_stdout(StringIO()):
                run_tuning.main(["--days", "7", "--walk-forward"])

        self.assertEqual(seen_days, [3, 4])

    def test_report_ranks_profitable_grid_runs(self):
        def fake_run_backtest(**kwargs):
            short_delta = kwargs["config"]["strategies"]["vertical_spread"]["short_delta"]
            if short_delta == -0.2:
                result = BacktestResult(
                    starting_fund=kwargs["starting_fund"],
                    ending_equity=kwargs["starting_fund"] + 250.0,
                    total_return_pct=2.5,
                    sharpe=1.2,
                    max_drawdown_pct=1.0,
                    win_rate=60.0,
                    trade_count=5,
                    closed_trade_count=5,
                )
            else:
                result = BacktestResult(
                    starting_fund=kwargs["starting_fund"],
                    ending_equity=kwargs["starting_fund"] - 100.0,
                    total_return_pct=-1.0,
                    sharpe=-0.5,
                    max_drawdown_pct=3.0,
                    win_rate=20.0,
                    trade_count=5,
                    closed_trade_count=5,
                )
            return result, None

        with tempfile.TemporaryDirectory() as tmp:
            config = run_tuning.load_config()
            config["reporting"]["reports_dir"] = tmp
            stdout = StringIO()
            with (
                patch.object(run_tuning, "load_config", return_value=config),
                patch.object(run_tuning, "run_backtest", side_effect=fake_run_backtest),
                redirect_stdout(stdout),
            ):
                run_tuning.main(
                    [
                        "--grid",
                        "strategies.vertical_spread.short_delta=-0.25,-0.2",
                        "--report",
                        "--min-return-pct",
                        "0.1",
                        "--min-trades",
                        "1",
                    ]
            )
            payload = json.loads(stdout.getvalue())
            report_path = Path(payload["report_path"])
            report_exists = report_path.exists()
            report = report_path.read_text(encoding="utf-8")

        self.assertTrue(report_exists)
        self.assertEqual(payload["ranked_runs"][0]["return_pct"], 2.5)
        self.assertEqual(len(payload["profitable_runs"]), 1)
        self.assertIn("# HawksOptions Tuning Report", report)
        self.assertIn("run-2", report)

    def test_tuning_report_reuses_filename_timestamp_in_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = run_tuning._write_tuning_report(runs=[], output_dir=Path(tmp), top=1)
            payload = json.loads(path.read_text(encoding="utf-8"))

        filename_timestamp = path.stem.removeprefix("tuning_")
        payload_timestamp = datetime.fromisoformat(payload["generated_at"]).strftime("%Y%m%d-%H%M%S")
        self.assertEqual(payload_timestamp, filename_timestamp)


if __name__ == "__main__":
    unittest.main()
