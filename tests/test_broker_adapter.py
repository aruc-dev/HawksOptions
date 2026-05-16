from __future__ import annotations

import unittest
from datetime import date
from pathlib import Path
from typing import Any

from core.broker_adapter import MarketDataClient, OrderSubmissionClient, TradingClient
from core.models import OptionContract, OrderLeg, StrategyOrder
from core.order_executor import execute_order


def _order() -> StrategyOrder:
    contract = OptionContract(
        contract_symbol="SPY260619P00500000",
        underlying="SPY",
        option_type="put",
        strike=500.0,
        expiration=date(2026, 6, 19),
        bid=1.0,
        ask=1.1,
        open_interest=500,
        volume=50,
        implied_volatility=0.2,
        delta=-0.2,
        underlying_price=520.0,
    )
    return StrategyOrder(
        strategy_name="cash_secured_put",
        strategy_id="cash-secured-put-SPY-20260423",
        underlying="SPY",
        legs=[OrderLeg(contract=contract, side="sell_to_open")],
        max_loss=50000.0,
        max_profit=105.0,
        required_buying_power=50000.0,
        profit_take_pct=0.5,
        loss_stop_multiple=2.0,
        roll_threshold_delta=-0.4,
        iv_rank=50.0,
        required_options_level=1,
    )


class FakeBroker:
    def __init__(self) -> None:
        self.submitted_payloads: list[dict[str, Any]] = []

    def get_underlying_snapshot(self, symbol: str, as_of: date | None = None) -> dict[str, Any]:
        return {"symbol": symbol, "price": 520.0, "iv_rank": 50.0, "realized_vol_20d": 0.2, "atr_pct": 0.02}

    def get_option_chain(self, symbol: str, as_of: date | None = None) -> list[OptionContract]:
        return [_order().legs[0].contract]

    def get_option_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        return {
            symbol: {"bid": 1.0, "ask": 1.1, "source": "fake_broker"}
            for symbol in symbols
        }

    def get_account(self) -> dict[str, Any]:
        return {"equity": 100000.0, "portfolio_value": 100000.0, "cash": 90000.0, "buying_power": 200000.0}

    def get_positions(self) -> list[dict[str, Any]]:
        return []

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.submitted_payloads.append(payload)
        return {
            "id": "fake-1",
            "status": "accepted",
            "payload": payload,
            "legs": [
                {
                    "symbol": payload["symbol"],
                    "filled_avg_price": payload["limit_price"],
                    "filled_qty": payload["qty"],
                }
            ],
        }


class BrokerAdapterTests(unittest.TestCase):
    def test_fake_broker_satisfies_repo_owned_protocols(self):
        broker = FakeBroker()

        self.assertIsInstance(broker, MarketDataClient)
        self.assertIsInstance(broker, OrderSubmissionClient)
        self.assertIsInstance(broker, TradingClient)

    def test_order_executor_submits_through_protocol_without_alpaca_client(self):
        broker = FakeBroker()

        result = execute_order(broker, _order(), dry_run=False)

        self.assertEqual(result["id"], "fake-1")
        self.assertEqual(broker.submitted_payloads[0]["type"], "limit")
        self.assertIn("execution_quality", result)
        self.assertIn("limit_price_improvement", result)

    def test_strategy_risk_and_backtest_layers_do_not_import_alpaca_adapter(self):
        root = Path(__file__).resolve().parent.parent
        checked_paths = [
            *sorted((root / "strategies").glob("*.py")),
            root / "core" / "risk_manager.py",
            root / "core" / "backtest_engine.py",
            root / "core" / "order_executor.py",
        ]

        offenders = [
            str(path.relative_to(root))
            for path in checked_paths
            if "alpaca" in path.read_text(encoding="utf-8").lower()
        ]

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
