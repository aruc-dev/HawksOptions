from __future__ import annotations

import unittest
from datetime import date

from core.alpaca_options_client import AlpacaOptionsClient
from core.config import load_config, load_underlyings
from core.models import StrategyContext
from strategies.cash_secured_put import CashSecuredPutStrategy


class CashSecuredPutStrategyTests(unittest.TestCase):
    def test_generates_order_for_allowed_underlying(self):
        config = load_config()
        config["market_data"]["use_sample_data"] = True
        config["strategies"]["cash_secured_put"]["enabled"] = True
        client = AlpacaOptionsClient(config, use_sample_data=True)
        underlying = {**load_underlyings(config)[0], "strategies_allowed": ["cash_secured_put"]}
        snapshot = client.get_underlying_snapshot(underlying["symbol"], as_of=date(2026, 4, 23))
        context = StrategyContext(
            underlying=underlying,
            chain=client.get_option_chain(underlying["symbol"], as_of=date(2026, 4, 23)),
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=snapshot["iv_rank"],
            as_of=date(2026, 4, 23),
            underlying_price=snapshot["price"],
            next_earnings_date=None,
        )
        order = CashSecuredPutStrategy(config).generate_order(context)
        self.assertIsNotNone(order)
        self.assertEqual(order.strategy_name, "cash_secured_put")


if __name__ == "__main__":
    unittest.main()
