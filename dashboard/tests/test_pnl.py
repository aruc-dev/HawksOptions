from __future__ import annotations

import unittest
from datetime import datetime, timezone

from dashboard import pnl


class DashboardPnlTests(unittest.TestCase):
    def test_realized_pnl_for_short_row(self):
        row = {"entry_price": "1.20", "exit_price": "0.40", "qty": "1", "side": "sell_to_open"}
        self.assertEqual(pnl.realized_pnl_for_row(row), 80.0)

    def test_realized_pnl_window_counts_wins_losses(self):
        now = datetime(2026, 4, 23, tzinfo=timezone.utc)
        rows = [
            {"timestamp": "2026-04-22T12:00:00+00:00", "status": "closed", "entry_price": "1.20", "exit_price": "0.40", "qty": "1", "side": "sell_to_open"},
            {"timestamp": "2026-04-21T12:00:00+00:00", "status": "closed", "entry_price": "1.00", "exit_price": "1.50", "qty": "1", "side": "sell_to_open"},
        ]
        summary = pnl.realized_pnl_window(rows, lookback_days=7, now_utc=now)
        self.assertEqual(summary["wins"], 1)
        self.assertEqual(summary["losses"], 1)

    def test_daily_loss_headroom_status(self):
        baseline = {"portfolio_value": 10000.0}
        headroom = pnl.daily_loss_headroom(baseline, 9600.0, 0.05)
        self.assertEqual(headroom["status"], "critical")


if __name__ == "__main__":
    unittest.main()
