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

    def test_tuning_report_reuses_filename_timestamp_in_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = run_tuning._write_tuning_report(runs=[], output_dir=Path(tmp), top=1)
            payload = json.loads(path.read_text(encoding="utf-8"))

        filename_timestamp = path.stem.removeprefix("tuning_")
        payload_timestamp = datetime.fromisoformat(payload["generated_at"]).strftime("%Y%m%d-%H%M%S")
        self.assertEqual(payload_timestamp, filename_timestamp)


if __name__ == "__main__":
    unittest.main()
