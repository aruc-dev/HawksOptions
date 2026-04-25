from __future__ import annotations

import unittest

from core.backtest_engine import run_backtest
from core.config import load_config
from strategies import build_enabled_strategies


class RunBacktestTests(unittest.TestCase):
    def test_backtest_executes_trades(self):
        config = load_config()
        result, report_path = run_backtest(config=config, strategies=build_enabled_strategies(config), days=10, starting_fund=10000.0)
        self.assertGreater(result.trade_count, 0)
        self.assertGreater(result.closed_trade_count, 0)
        self.assertTrue(report_path.exists())


if __name__ == "__main__":
    unittest.main()
