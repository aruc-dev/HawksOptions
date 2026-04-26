from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from core.backtest_engine import _apply_entry_slippage, _leg_slippage_cost, _mark_to_market, run_backtest
from core.config import load_config
from core.models import OptionContract, OrderLeg, PositionSnapshot
from strategies import build_enabled_strategies


def _contract(symbol: str, bid: float, ask: float) -> OptionContract:
    return OptionContract(
        contract_symbol=symbol,
        underlying="SPY",
        option_type="put",
        strike=500.0,
        expiration=date(2026, 6, 19),
        bid=bid,
        ask=ask,
        open_interest=500,
        volume=50,
        underlying_price=510.0,
    )


class _StaticChainClient:
    def __init__(self, chain: list[OptionContract]):
        self._chain = chain

    def get_option_chain(self, underlying: str, as_of=None) -> list[OptionContract]:
        return self._chain


class RunBacktestTests(unittest.TestCase):
    def test_backtest_executes_trades(self):
        config = load_config()
        result, report_path = run_backtest(config=config, strategies=build_enabled_strategies(config), days=10, starting_fund=10000.0)
        self.assertGreater(result.trade_count, 0)
        self.assertGreater(result.closed_trade_count, 0)
        self.assertTrue(report_path.exists())

    def test_slippage_is_charged_on_entry_and_exit_prices(self):
        sell_leg = OrderLeg(contract=_contract("SPY260619P00500000", 1.00, 1.20), side="sell_to_open")
        buy_leg = OrderLeg(contract=_contract("SPY260619P00495000", 0.40, 0.50), side="buy_to_open")
        slippage = {"per_leg_cents": 0.02, "spread_pct": 0.5, "commission_per_contract": 0.65}
        position = PositionSnapshot(
            strategy_id="vertical_spread-SPY-20260423",
            strategy_name="vertical_spread",
            underlying="SPY",
            legs=[sell_leg, buy_leg],
            opened_at=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
            entry_credit=sum(leg.opening_cashflow() for leg in [sell_leg, buy_leg]),
            max_loss=100.0,
            profit_take_pct=0.5,
            loss_stop_multiple=2.0,
            roll_threshold_delta=-0.4,
        )

        self.assertEqual(_leg_slippage_cost(sell_leg, slippage), 7.65)
        self.assertEqual(_leg_slippage_cost(buy_leg, slippage), 5.15)

        _apply_entry_slippage(position, slippage)
        _mark_to_market(
            position,
            _StaticChainClient([sell_leg.contract, buy_leg.contract]),
            date(2026, 4, 24),
            slippage=slippage,
        )

        self.assertEqual(position.entry_credit, 52.2)
        self.assertEqual(position.current_close_cost, 77.8)
        self.assertEqual(position.current_pnl, -25.6)


if __name__ == "__main__":
    unittest.main()
