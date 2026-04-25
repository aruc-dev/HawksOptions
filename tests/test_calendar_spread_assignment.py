"""Tests for the calendar-spread early-assignment guard."""

from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from core.assignment_handler import calendar_front_assignment_risk
from core.models import OptionContract, OrderLeg, PositionSnapshot
from core.risk_manager import continuous_risk_checks


def _calendar_position(*, front_mid: float, underlying: float = 105.0, strike: float = 100.0) -> PositionSnapshot:
    """Build a calendar with a configurable front-leg mid price.

    Front leg expires earlier than back leg; both are calls at the
    same strike. ``front_mid`` is set via bid/ask so ``mid_price``
    returns the desired value.
    """
    front = OptionContract(
        contract_symbol="XYZ260619C00100000",
        underlying="XYZ",
        option_type="call",
        strike=strike,
        expiration=date(2026, 6, 19),
        bid=front_mid - 0.05,
        ask=front_mid + 0.05,
        underlying_price=underlying,
    )
    back = OptionContract(
        contract_symbol="XYZ260918C00100000",
        underlying="XYZ",
        option_type="call",
        strike=strike,
        expiration=date(2026, 9, 18),
        bid=10.0,
        ask=10.2,
        underlying_price=underlying,
    )
    return PositionSnapshot(
        strategy_id="cal-1",
        strategy_name="calendar_spread",
        underlying="XYZ",
        legs=[
            OrderLeg(contract=front, side="sell_to_open", qty=1),
            OrderLeg(contract=back, side="buy_to_open", qty=1),
        ],
        opened_at=datetime.now(timezone.utc),
        entry_credit=-450.0,  # debit position
        max_loss=450.0,
        profit_take_pct=0.30,
        loss_stop_multiple=1.0,
        roll_threshold_delta=None,
    )


class CalendarFrontAssignmentRiskTests(unittest.TestCase):
    def test_flags_when_front_at_intrinsic(self):
        """Underlying $105, strike $100 -> intrinsic $5. Front mid at
        $5 means a holder of the long side can exercise for free."""
        position = _calendar_position(front_mid=5.0)
        self.assertTrue(calendar_front_assignment_risk(position))

    def test_flags_when_front_below_intrinsic(self):
        position = _calendar_position(front_mid=4.5)
        self.assertTrue(calendar_front_assignment_risk(position))

    def test_does_not_flag_with_extrinsic_buffer(self):
        position = _calendar_position(front_mid=6.5)  # $5 intrinsic + $1.5 extrinsic
        self.assertFalse(calendar_front_assignment_risk(position))

    def test_slippage_band_widens_threshold(self):
        position = _calendar_position(front_mid=5.10)
        self.assertFalse(calendar_front_assignment_risk(position, slippage=0.05))
        self.assertTrue(calendar_front_assignment_risk(position, slippage=0.20))

    def test_no_flag_for_otm_calendar(self):
        # Underlying below strike -> intrinsic 0; front mid > 0 means
        # the option still has time value.
        position = _calendar_position(front_mid=1.50, underlying=95.0)
        self.assertFalse(calendar_front_assignment_risk(position))

    def test_only_applies_to_calendar_spreads(self):
        position = _calendar_position(front_mid=4.5)
        position.strategy_name = "iron_condor"
        self.assertFalse(calendar_front_assignment_risk(position))

    def test_continuous_risk_check_emits_action(self):
        position = _calendar_position(front_mid=4.8)
        payload = continuous_risk_checks(
            [position],
            config={"gates": {"calendar_assignment_slippage": 0.05}},
            as_of=datetime(2026, 4, 25, 16, 0, tzinfo=timezone.utc),
        )
        actions = {item["action"] for item in payload["actions"] if item["strategy_id"] == "cal-1"}
        self.assertIn("close_for_calendar_assignment", actions)


if __name__ == "__main__":
    unittest.main()
