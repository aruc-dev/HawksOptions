from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from core.models import OptionContract, OrderLeg, StrategyOrder
from core.order_executor import build_order_payload, persist_open_order


def _order() -> StrategyOrder:
    short = OptionContract(
        contract_symbol="SPY260619P00500000",
        underlying="SPY",
        option_type="put",
        strike=500.0,
        expiration=date(2026, 6, 19),
        bid=1.0,
        ask=1.1,
        open_interest=500,
        volume=20,
        implied_volatility=0.2,
        delta=-0.2,
        theta=-0.1,
        vega=0.2,
        gamma=0.01,
        underlying_price=520.0,
    )
    long = OptionContract(
        contract_symbol="SPY260619P00499000",
        underlying="SPY",
        option_type="put",
        strike=499.0,
        expiration=date(2026, 6, 19),
        bid=0.8,
        ask=0.9,
        open_interest=500,
        volume=20,
        implied_volatility=0.2,
        delta=-0.18,
        theta=-0.1,
        vega=0.2,
        gamma=0.01,
        underlying_price=520.0,
    )
    return StrategyOrder(
        strategy_name="vertical_spread",
        strategy_id="vertical_spread-SPY-20260423",
        underlying="SPY",
        legs=[OrderLeg(contract=short, side="sell_to_open"), OrderLeg(contract=long, side="buy_to_open")],
        max_loss=80.0,
        max_profit=20.0,
        required_buying_power=80.0,
        profit_take_pct=0.5,
        loss_stop_multiple=1.5,
        roll_threshold_delta=-0.4,
        iv_rank=50.0,
        required_options_level=3,
    )


class OrderExecutorTests(unittest.TestCase):
    def test_build_multileg_payload(self):
        payload = build_order_payload(_order())
        self.assertEqual(payload["order_class"], "mleg")
        self.assertEqual(len(payload["legs"]), 2)

    def test_persist_open_order_writes_trade_log_and_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            trade_log = Path(tmp) / "trades.csv"
            positions = Path(tmp) / "positions.json"
            snapshot = persist_open_order(
                order=_order(),
                mode="paper",
                order_id="ord-1",
                trade_log_path=trade_log,
                positions_path=positions,
            )
            self.assertTrue(trade_log.exists())
            self.assertTrue(positions.exists())
            self.assertEqual(snapshot.strategy_id, "vertical_spread-SPY-20260423")


if __name__ == "__main__":
    unittest.main()
