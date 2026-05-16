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

    def test_slippage_summary_groups_by_strategy(self):
        now = datetime(2026, 4, 23, tzinfo=timezone.utc)
        rows = [
            {"timestamp": "2026-04-22T12:00:00+00:00", "strategy": "iron_condor", "leg_slippage_dollars": "2.50"},
            {"timestamp": "2026-04-22T12:00:00+00:00", "strategy": "iron_condor", "leg_slippage_dollars": "1.50"},
            {"timestamp": "2026-04-22T12:00:00+00:00", "strategy": "vertical_spread", "leg_slippage_dollars": "1.00"},
        ]

        summary = pnl.slippage_summary(rows, lookback_days=7, now_utc=now)

        self.assertEqual(summary[0]["strategy"], "iron_condor")
        self.assertEqual(summary[0]["total_slippage_dollars"], 4.0)
        self.assertEqual(summary[0]["average_slippage_dollars"], 2.0)


if __name__ == "__main__":
    unittest.main()
