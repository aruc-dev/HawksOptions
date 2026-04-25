from __future__ import annotations

import unittest
from datetime import date

from core.contract_selector import find_by_delta, select_vertical_spread
from core.models import OptionContract


def _contract(symbol: str, option_type: str, strike: float, delta: float) -> OptionContract:
    return OptionContract(
        contract_symbol=symbol,
        underlying="SPY",
        option_type=option_type,
        strike=strike,
        expiration=date(2026, 6, 19),
        bid=1.0,
        ask=1.05,
        open_interest=500,
        volume=50,
        implied_volatility=0.2,
        delta=delta,
        theta=-0.1,
        vega=0.2,
        gamma=0.01,
        underlying_price=520,
    )


class ContractSelectorTests(unittest.TestCase):
    def test_find_by_delta(self):
        chain = [
            _contract("A", "put", 500, -0.32),
            _contract("B", "put", 495, -0.21),
            _contract("C", "put", 490, -0.14),
        ]
        selected = find_by_delta(chain, -0.20, as_of=date(2026, 5, 1))
        self.assertEqual(selected.contract_symbol, "B")

    def test_vertical_spread_prefers_closest_protective_leg(self):
        chain = [
            _contract("SHORT", "put", 500, -0.25),
            _contract("LONG1", "put", 499, -0.22),
            _contract("LONG2", "put", 495, -0.10),
        ]
        short_leg, long_leg = select_vertical_spread(
            chain,
            short_delta=-0.25,
            long_delta=-0.10,
            option_type="put",
            as_of=date(2026, 5, 1),
        )
        self.assertEqual(short_leg.contract_symbol, "SHORT")
        self.assertEqual(long_leg.contract_symbol, "LONG1")


if __name__ == "__main__":
    unittest.main()
