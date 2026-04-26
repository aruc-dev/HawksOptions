from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import date, datetime

from core.alpaca_options_client import AlpacaOptionsClient
from core.backtest_engine import _apply_expiration_assignment, _portfolio_equity, run_backtest
from core.config import load_config, load_underlyings
from core.models import OptionContract, OrderLeg, PositionSnapshot, StrategyContext, StrategyOrder
from scheduler.common import build_context
from strategies import build_enabled_strategies
from strategies.cash_secured_put import CashSecuredPutStrategy
from strategies.iron_condor import IronCondorStrategy
from strategies.selection import score_order
from strategies.vertical_spread import VerticalSpreadStrategy


def _contract(symbol: str = "SPY260528P00095000") -> OptionContract:
    return OptionContract(
        contract_symbol=symbol,
        underlying="SPY",
        option_type="put",
        strike=95.0,
        expiration=date(2026, 5, 28),
        bid=1.0,
        ask=1.05,
        open_interest=500,
        volume=50,
        implied_volatility=0.3,
        delta=-0.2,
        theta=-0.04,
        vega=0.1,
        gamma=0.01,
        underlying_price=100.0,
    )


def _context(config: dict, underlying: dict | None = None, *, current_iv: float = 0.3) -> StrategyContext:
    underlying = underlying or {"symbol": "SPY", "strategies_allowed": ["cash_secured_put"]}
    return StrategyContext(
        underlying=underlying,
        chain=[_contract()],
        config=config,
        account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
        iv_rank=55.0,
        as_of=date(2026, 4, 23),
        underlying_price=100.0,
        current_iv=current_iv,
    )


class _InventoryClient(AlpacaOptionsClient):
    def get_positions(self):
        return [{"symbol": "SPY", "qty": 200, "avg_entry_price": 101.25}]


class _SnapshotClient:
    def get_underlying_snapshot(self, symbol, as_of=None):
        return {"symbol": symbol, "price": 92.0}


class StrategyTodoTests(unittest.TestCase):
    def test_strategy_weight_changes_selection_score(self):
        config = load_config()
        context = _context(config)
        low = StrategyOrder("cash_secured_put", "low", "SPY", [], 100.0, 20.0, 100.0, 0.5, 2.0, None, 55.0)
        high = StrategyOrder("iron_condor", "high", "SPY", [], 100.0, 20.0, 100.0, 0.5, 2.0, None, 55.0)
        config["strategies"]["cash_secured_put"]["weight"] = 0.25
        config["strategies"]["iron_condor"]["weight"] = 2.0

        self.assertGreater(score_order(high, context, config), score_order(low, context, config))

    def test_configured_contract_limits_cap_generated_quantity(self):
        config = deepcopy(load_config())
        config["strategies"]["cash_secured_put"]["contracts"] = 4
        config["strategies"]["cash_secured_put"]["max_contracts_per_underlying"] = 3
        underlying = {"symbol": "SPY", "max_contracts": 2, "strategies_allowed": ["cash_secured_put"]}

        order = CashSecuredPutStrategy(config).generate_order(_context(config, underlying))

        self.assertIsNotNone(order)
        self.assertEqual(order.legs[0].qty, 2)

    def test_negative_contract_limits_do_not_crash_quantity(self):
        config = deepcopy(load_config())
        config["strategies"]["cash_secured_put"]["contracts"] = -1
        config["strategies"]["cash_secured_put"]["max_contracts_per_underlying"] = -1
        underlying = {"symbol": "SPY", "strategies_allowed": ["cash_secured_put"]}

        order = CashSecuredPutStrategy(config).generate_order(_context(config, underlying))

        self.assertIsNone(order)

    def test_current_iv_context_controls_iron_condor_regime_filter(self):
        config = deepcopy(load_config())
        config["strategies"]["iron_condor"]["min_iv_rank"] = 0
        config["strategies"]["iron_condor"]["min_credit_to_width"] = 0
        config["strategies"]["iron_condor"]["min_net_credit"] = 0
        expiration = date(2026, 6, 2)
        underlying = {"symbol": "SPY", "strategies_allowed": ["iron_condor"]}
        chain = [
            OptionContract("SPY260602P00095000", "SPY", "put", 95.0, expiration, 1.2, 1.3, open_interest=500, volume=50, delta=-0.18, underlying_price=100.0),
            OptionContract("SPY260602P00090000", "SPY", "put", 90.0, expiration, 0.3, 0.35, open_interest=500, volume=50, delta=-0.08, underlying_price=100.0),
            OptionContract("SPY260602C00105000", "SPY", "call", 105.0, expiration, 1.1, 1.2, open_interest=500, volume=50, delta=0.18, underlying_price=100.0),
            OptionContract("SPY260602C00110000", "SPY", "call", 110.0, expiration, 0.25, 0.3, open_interest=500, volume=50, delta=0.08, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying=underlying,
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=10.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.30,
            realized_vol_20d=0.20,
            atr_pct=0.01,
        )

        self.assertIsNotNone(IronCondorStrategy(config).generate_order(context))

    def test_credit_quality_gate_blocks_low_quality_vertical(self):
        config = deepcopy(load_config())
        config["strategies"]["vertical_spread"]["enabled"] = True
        config["strategies"]["vertical_spread"]["min_credit_to_width"] = 0.9
        client = AlpacaOptionsClient(config, use_sample_data=True)
        underlying = load_underlyings(config)[0]
        snapshot = client.get_underlying_snapshot(underlying["symbol"], as_of=date(2026, 4, 23))
        context = StrategyContext(
            underlying=underlying,
            chain=client.get_option_chain(underlying["symbol"], as_of=date(2026, 4, 23)),
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=snapshot["iv_rank"],
            as_of=date(2026, 4, 23),
            underlying_price=snapshot["price"],
            current_iv=snapshot["current_iv"],
        )

        self.assertIsNone(VerticalSpreadStrategy(config).generate_order(context))

    def test_stock_inventory_feeds_covered_call_context(self):
        config = load_config()
        client = _InventoryClient(config, use_sample_data=True)
        underlying = {"symbol": "SPY", "strategies_allowed": ["covered_call"]}

        context = build_context(
            config=config,
            client=client,
            underlying=underlying,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertEqual(context.long_shares, 200)
        self.assertEqual(context.cost_basis, 101.25)

    def test_assignment_stock_inventory_is_included_in_equity(self):
        contract = OptionContract(
            contract_symbol="SPY260423P00095000",
            underlying="SPY",
            option_type="put",
            strike=95.0,
            expiration=date(2026, 4, 23),
            bid=3.0,
            ask=3.0,
            open_interest=500,
            volume=50,
            underlying_price=92.0,
        )
        position = PositionSnapshot(
            strategy_id="csp-SPY-20260423",
            strategy_name="cash_secured_put",
            underlying="SPY",
            legs=[OrderLeg(contract=contract, side="sell_to_open")],
            opened_at=datetime(2026, 4, 23),
            entry_credit=300.0,
            max_loss=9200.0,
            profit_take_pct=0.5,
            loss_stop_multiple=2.0,
            roll_threshold_delta=-0.4,
        )
        position.current_pnl = 0.0
        inventory = {}

        cash_delta = _apply_expiration_assignment(position, inventory, date(2026, 4, 23))
        equity = _portfolio_equity(
            cash_balance=100000.0 + cash_delta,
            stock_inventory=inventory,
            open_positions=[],
            client=_SnapshotClient(),
            as_of=date(2026, 4, 23),
        )

        self.assertEqual(inventory["SPY"]["shares"], 100.0)
        self.assertEqual(equity, 100000.0)

    def test_fixture_backtest_replays_historical_option_universe(self):
        config = deepcopy(load_config())
        config["underlyings"]["source"] = "tests/fixtures/backtest_underlyings.yaml"
        config["backtest"]["data_source"] = "fixture"
        config["backtest"]["fixture_file"] = "tests/fixtures/backtest_market_data.json"
        config["strategies"]["iron_condor"]["min_net_credit"] = 0
        config["strategies"]["iron_condor"]["min_credit_to_width"] = 0

        result, _ = run_backtest(
            config=config,
            strategies=build_enabled_strategies(config),
            days=2,
            starting_fund=200000.0,
            start_date=date(2026, 4, 20),
        )

        self.assertGreater(result.trade_count, 0)


if __name__ == "__main__":
    unittest.main()
