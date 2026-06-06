from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import date

from core.alpaca_options_client import AlpacaOptionsClient
from core.config import load_config, load_underlyings
from core.models import StrategyContext
from strategies.vertical_spread import VerticalSpreadStrategy


class VerticalSpreadStrategyTests(unittest.TestCase):
    def test_generates_defined_risk_spread(self):
        config = deepcopy(load_config())
        config["strategies"]["vertical_spread"]["enabled"] = True
        client = AlpacaOptionsClient(config, use_sample_data=True)
        underlying = load_underlyings(config)[0]
        snapshot = client.get_underlying_snapshot(underlying["symbol"], as_of=date(2026, 4, 23))
        context = StrategyContext(
            underlying=underlying,
            chain=client.get_option_chain(underlying["symbol"], as_of=date(2026, 4, 23)),
            config=config,
            account={"equity": 10000.0, "portfolio_value": 10000.0, "cash": 10000.0, "buying_power": 20000.0},
            iv_rank=snapshot["iv_rank"],
            as_of=date(2026, 4, 23),
            underlying_price=snapshot["price"],
            next_earnings_date=None,
        )
        order = VerticalSpreadStrategy(config).generate_order(context)
        self.assertIsNotNone(order)
        self.assertGreater(order.max_loss, 0)
        self.assertLess(order.max_loss, 500)

    def test_past_earnings_date_does_not_block_generation(self):
        config = deepcopy(load_config())
        config["strategies"]["vertical_spread"]["enabled"] = True
        client = AlpacaOptionsClient(config, use_sample_data=True)
        underlying = {
            **load_underlyings(config)[0],
            "strategies_allowed": ["vertical_spread"],
            "next_earnings_date": "2026-04-01",
        }
        snapshot = client.get_underlying_snapshot(underlying["symbol"], as_of=date(2026, 4, 23))
        context = StrategyContext(
            underlying=underlying,
            chain=client.get_option_chain(underlying["symbol"], as_of=date(2026, 4, 23)),
            config=config,
            account={"equity": 10000.0, "portfolio_value": 10000.0, "cash": 10000.0, "buying_power": 20000.0},
            iv_rank=snapshot["iv_rank"],
            as_of=date(2026, 4, 23),
            underlying_price=snapshot["price"],
            next_earnings_date=date(2026, 4, 1),
        )

        order = VerticalSpreadStrategy(config).generate_order(context)

        self.assertIsNotNone(order)
        self.assertIsNone(order.next_earnings_date)


if __name__ == "__main__":
    unittest.main()
