from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from core.assignment_handler import detect_assignments, should_close_short_call_for_ex_div
from core.models import OptionContract, OrderLeg, PositionSnapshot


def _position() -> PositionSnapshot:
    contract = OptionContract(
        contract_symbol="AAPL260619C00210000",
        underlying="AAPL",
        option_type="call",
        strike=210.0,
        expiration=date(2026, 6, 19),
        bid=1.0,
        ask=1.1,
        open_interest=500,
        volume=30,
        implied_volatility=0.25,
        delta=0.48,
        theta=-0.1,
        vega=0.2,
        gamma=0.01,
        underlying_price=215.0,
    )
    return PositionSnapshot(
        strategy_id="cc-1",
        strategy_name="covered_call",
        underlying="AAPL",
        legs=[OrderLeg(contract=contract, side="sell_to_open")],
        opened_at=datetime.now(timezone.utc),
        entry_credit=90.0,
        max_loss=20000.0,
        profit_take_pct=0.5,
        loss_stop_multiple=2.0,
        roll_threshold_delta=0.45,
        ex_dividend_date=date(2026, 4, 24),
        dividend_amount=0.35,
        remaining_extrinsic_value=0.1,
        short_leg_itm=True,
    )


class AssignmentHandlerTests(unittest.TestCase):
    def test_close_for_ex_div(self):
        self.assertTrue(should_close_short_call_for_ex_div(_position(), as_of=date(2026, 4, 23)))

    def test_detect_assignment(self):
        events = detect_assignments([_position()], current_symbols=["AAPL"])
        self.assertEqual(events[0]["event"], "assignment_detected")


if __name__ == "__main__":
    unittest.main()
