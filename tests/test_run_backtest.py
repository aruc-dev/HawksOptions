from __future__ import annotations

import copy
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

    def test_slippage_reduces_returns(self):
        """Backtests with slippage should produce lower (or equal)
        equity than the same backtest with zero slippage."""
        zero_slip = copy.deepcopy(load_config())
        zero_slip["backtest"] = {
            "slippage": {"per_leg_cents": 0.0, "spread_pct": 0.0, "commission_per_contract": 0.0}
        }
        heavy_slip = copy.deepcopy(zero_slip)
        heavy_slip["backtest"] = {
            "slippage": {"per_leg_cents": 0.10, "spread_pct": 1.0, "commission_per_contract": 1.00}
        }

        baseline, _ = run_backtest(
            config=zero_slip,
            strategies=build_enabled_strategies(zero_slip),
            days=10,
            starting_fund=10000.0,
        )
        with_slip, _ = run_backtest(
            config=heavy_slip,
            strategies=build_enabled_strategies(heavy_slip),
            days=10,
            starting_fund=10000.0,
        )
        self.assertLessEqual(with_slip.ending_equity, baseline.ending_equity)


if __name__ == "__main__":
    unittest.main()
