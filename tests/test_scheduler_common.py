from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from core.models import OptionContract, OrderLeg, PositionSnapshot
from scheduler.common import refresh_positions


class _FakeClient:
    def __init__(self, chain):
        self.chain = chain

    def get_option_chain(self, underlying, as_of=None):
        return self.chain


class RefreshPositionsTests(unittest.TestCase):
    def test_debit_spread_refresh_allows_negative_close_cost(self):
        front = OptionContract(
            contract_symbol="XYZ260619C00100000",
            underlying="XYZ",
            option_type="call",
            strike=100.0,
            expiration=date(2026, 6, 19),
            bid=1.0,
            ask=1.0,
            underlying_price=105.0,
        )
        back = OptionContract(
            contract_symbol="XYZ260918C00100000",
            underlying="XYZ",
            option_type="call",
            strike=100.0,
            expiration=date(2026, 9, 18),
            bid=4.0,
            ask=4.0,
            underlying_price=105.0,
        )
        position = PositionSnapshot(
            strategy_id="cal-1",
            strategy_name="calendar_spread",
            underlying="XYZ",
            legs=[
                OrderLeg(contract=front, side="sell_to_open"),
                OrderLeg(contract=back, side="buy_to_open"),
            ],
            opened_at=datetime.now(timezone.utc),
            entry_credit=-200.0,
            max_loss=200.0,
            profit_take_pct=0.3,
            loss_stop_multiple=1.0,
            roll_threshold_delta=None,
        )

        refreshed = refresh_positions(
            [position],
            client=_FakeClient([front, back]),
            as_of=date(2026, 4, 23),
        )[0]

        self.assertEqual(refreshed.current_close_cost, -300.0)
        self.assertEqual(refreshed.current_pnl, 100.0)


if __name__ == "__main__":
    unittest.main()
