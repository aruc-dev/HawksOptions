from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from core.models import OptionContract, OrderLeg, PositionSnapshot
from core.roll_engine import should_roll_position


def _snapshot(delta: float = -0.45, roll_count: int = 0) -> PositionSnapshot:
    contract = OptionContract(
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
        delta=delta,
        theta=-0.1,
        vega=0.2,
        gamma=0.01,
        underlying_price=520.0,
    )
    return PositionSnapshot(
        strategy_id="spread-1",
        strategy_name="vertical_spread",
        underlying="SPY",
        legs=[OrderLeg(contract=contract, side="sell_to_open")],
        opened_at=datetime.now(timezone.utc),
        entry_credit=50.0,
        max_loss=50.0,
        profit_take_pct=0.5,
        loss_stop_multiple=1.5,
        roll_threshold_delta=-0.4,
        roll_count=roll_count,
    )


class RollEngineTests(unittest.TestCase):
    def test_roll_when_threshold_breached(self):
        decision = should_roll_position(_snapshot())
        self.assertTrue(decision.should_roll)

    def test_stop_when_max_rolls_reached(self):
        decision = should_roll_position(_snapshot(roll_count=2), max_rolls=2)
        self.assertFalse(decision.should_roll)


if __name__ == "__main__":
    unittest.main()
