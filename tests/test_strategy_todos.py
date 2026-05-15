from __future__ import annotations

import json
import unittest
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from core.alpaca_options_client import AlpacaOptionsClient
from core.backtest_engine import _apply_expiration_assignment, _portfolio_equity, run_backtest
from core.config import load_config, load_underlyings
from core.dealer_positioning import dealer_positioning_context
from core.event_risk import event_risk_context
from core.historical_market_data import HistoricalReplayClient
from core.models import OptionContract, OrderLeg, PositionSnapshot, StrategyContext, StrategyOrder
from core.open_interest_analytics import max_pain_strike, open_interest_context
from core.risk_manager import continuous_risk_checks, pre_trade_check
from scheduler.common import build_context
from strategies import build_enabled_strategies
from strategies.broken_wing_butterfly import BrokenWingButterflyStrategy
from strategies.butterfly import ButterflyStrategy
from strategies.cash_secured_put import CashSecuredPutStrategy
from strategies.collar import CollarStrategy
from strategies.diagonal_spread import DiagonalSpreadStrategy
from strategies.earnings_calendar_scanner import scan_earnings_calendar_candidates, scan_volatility_crush_iron_condor_candidates
from strategies.iron_condor import IronCondorStrategy
from strategies.selection import score_order
from strategies.tail_risk_hedge import TailRiskHedgeStrategy
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
        self.assertIn("implied_realized_spread", high.metadata["selection"])
        self.assertIn("put_tail_skew", high.metadata["selection"])
        self.assertIn("term_structure_slope", high.metadata["selection"])
        self.assertIn("max_pain_strike", high.metadata["selection"])
        self.assertIn("total_open_interest", high.metadata["selection"])
        self.assertEqual(high.metadata["selection"]["dealer_positioning"]["regime"], "unknown")
        self.assertIn("technical_regime", high.metadata["selection"])
        self.assertIn("event_risk", high.metadata["selection"])

    def test_expanded_selection_score_rewards_liquidity_and_dte_fit(self):
        config = load_config()
        context = _context(config)
        liquid_contract = _contract()
        illiquid_contract = replace(
            liquid_contract,
            contract_symbol="SPY260702P00095000",
            expiration=date(2026, 7, 2),
            bid=0.5,
            ask=1.5,
            open_interest=1,
            volume=0,
            theta=-0.01,
            gamma=0.05,
            vega=0.5,
        )
        liquid_order = StrategyOrder(
            "cash_secured_put",
            "liquid",
            "SPY",
            [OrderLeg(liquid_contract, "sell_to_open")],
            100.0,
            20.0,
            100.0,
            0.5,
            2.0,
            None,
            55.0,
        )
        illiquid_order = StrategyOrder(
            "cash_secured_put",
            "illiquid",
            "SPY",
            [OrderLeg(illiquid_contract, "sell_to_open")],
            100.0,
            20.0,
            100.0,
            0.5,
            2.0,
            None,
            55.0,
        )

        self.assertGreater(score_order(liquid_order, context, config), score_order(illiquid_order, context, config))
        self.assertGreater(liquid_order.metadata["selection"]["liquidity_score"], illiquid_order.metadata["selection"]["liquidity_score"])
        self.assertGreater(liquid_order.metadata["selection"]["dte_fit_score"], illiquid_order.metadata["selection"]["dte_fit_score"])
        self.assertIn("score_components", liquid_order.metadata["selection"])

    def test_expanded_selection_score_accounts_for_portfolio_greek_room(self):
        config = load_config()
        contract = _contract()
        order_with_room = StrategyOrder(
            "cash_secured_put",
            "room",
            "SPY",
            [OrderLeg(contract, "sell_to_open")],
            100.0,
            20.0,
            100.0,
            0.5,
            2.0,
            None,
            55.0,
        )
        order_near_limit = StrategyOrder(
            "cash_secured_put",
            "limit",
            "SPY",
            [OrderLeg(contract, "sell_to_open")],
            100.0,
            20.0,
            100.0,
            0.5,
            2.0,
            None,
            55.0,
        )
        context_with_room = _context(
            config,
            {
                "symbol": "SPY",
                "strategies_allowed": ["cash_secured_put"],
            },
        )
        context_near_limit = replace(
            context_with_room,
            account={
                **context_with_room.account,
                "portfolio_greeks": {"delta": 95.0},
                "greek_limits": {"delta": 100.0},
            },
        )

        self.assertGreater(
            score_order(order_with_room, context_with_room, config),
            score_order(order_near_limit, context_near_limit, config),
        )
        self.assertLess(order_near_limit.metadata["selection"]["portfolio_greek_room_score"], 1.0)

    def test_open_interest_context_computes_max_pain_profile(self):
        chain = [
            OptionContract("SPY260528C00095000", "SPY", "call", 95.0, date(2026, 5, 28), 1.0, 1.02, open_interest=100, volume=50, underlying_price=100.0),
            OptionContract("SPY260528P00095000", "SPY", "put", 95.0, date(2026, 5, 28), 1.0, 1.02, open_interest=10, volume=50, underlying_price=100.0),
            OptionContract("SPY260528C00100000", "SPY", "call", 100.0, date(2026, 5, 28), 1.0, 1.02, open_interest=50, volume=50, underlying_price=100.0),
            OptionContract("SPY260528P00100000", "SPY", "put", 100.0, date(2026, 5, 28), 1.0, 1.02, open_interest=50, volume=50, underlying_price=100.0),
            OptionContract("SPY260528C00105000", "SPY", "call", 105.0, date(2026, 5, 28), 1.0, 1.02, open_interest=10, volume=50, underlying_price=100.0),
            OptionContract("SPY260528P00105000", "SPY", "put", 105.0, date(2026, 5, 28), 1.0, 1.02, open_interest=100, volume=50, underlying_price=100.0),
        ]

        context = open_interest_context(chain, underlying_price=100.0)

        self.assertEqual(max_pain_strike(chain), 100.0)
        self.assertEqual(context["max_pain_strike"], 100.0)
        self.assertEqual(context["max_pain_distance_pct"], 0.0)
        self.assertEqual(context["largest_oi"], 110)
        self.assertEqual(context["total_open_interest"], 320)

    def test_open_interest_context_ignores_zero_effective_oi(self):
        chain = [
            OptionContract("SPY260528C00100000", "SPY", "call", 100.0, date(2026, 5, 28), 1.0, 1.02, open_interest=-10, volume=50, underlying_price=100.0),
        ]

        context = open_interest_context(chain, underlying_price=100.0)

        self.assertIsNone(max_pain_strike(chain))
        self.assertIsNone(context["max_pain_strike"])
        self.assertEqual(context["total_open_interest"], 0)

    def test_dealer_positioning_context_accepts_underlying_metadata(self):
        context = dealer_positioning_context(
            {
                "symbol": "SPY",
                "gamma_exposure": "-1250000",
                "gamma_flip_level": "101.5",
                "dealer_positioning_source": "unit_fixture",
            },
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
        )

        self.assertEqual(context["source"], "unit_fixture")
        self.assertEqual(context["gamma_exposure"], -1250000.0)
        self.assertEqual(context["gamma_flip_level"], 101.5)
        self.assertEqual(context["regime"], "negative_gamma")

    def test_dealer_positioning_context_does_not_change_selection_score(self):
        config = load_config()
        order_without = StrategyOrder("cash_secured_put", "plain", "SPY", [], 100.0, 20.0, 100.0, 0.5, 2.0, None, 55.0)
        order_with = StrategyOrder("cash_secured_put", "dealer", "SPY", [], 100.0, 20.0, 100.0, 0.5, 2.0, None, 55.0)
        base_context = _context(config)
        dealer_context = replace(
            base_context,
            underlying={
                **base_context.underlying,
                "gamma_exposure": -1000000.0,
                "gamma_flip_level": 101.0,
            },
        )

        self.assertEqual(score_order(order_without, base_context, config), score_order(order_with, dealer_context, config))
        self.assertEqual(order_with.metadata["selection"]["dealer_positioning"]["regime"], "negative_gamma")

    def test_technical_regime_filter_blocks_configured_trend_gate(self):
        config = deepcopy(load_config())
        config["strategies"]["cash_secured_put"]["min_trend_20d"] = 0.0
        underlying = {"symbol": "SPY", "strategies_allowed": ["cash_secured_put"], "trend_20d": -0.05}

        self.assertIsNone(CashSecuredPutStrategy(config).generate_order(_context(config, underlying)))
        self.assertIsNotNone(
            CashSecuredPutStrategy(config).generate_order(_context(config, {**underlying, "trend_20d": 0.02}))
        )

    def test_technical_regime_filter_blocks_missing_configured_metric(self):
        config = deepcopy(load_config())
        config["strategies"]["cash_secured_put"]["max_rsi_14"] = 70.0

        self.assertIsNone(CashSecuredPutStrategy(config).generate_order(_context(config)))
        self.assertIsNotNone(
            CashSecuredPutStrategy(config).generate_order(
                replace(_context(config), rsi_14=55.0)
            )
        )

    def test_event_risk_filter_blocks_when_configured(self):
        config = deepcopy(load_config())
        config["strategies"]["cash_secured_put"]["block_event_risk"] = True
        underlying = {
            "symbol": "SPY",
            "strategies_allowed": ["cash_secured_put"],
            "event_risk": True,
            "event_risk_reason": "fed_decision",
        }

        self.assertIsNone(CashSecuredPutStrategy(config).generate_order(_context(config, underlying)))
        self.assertEqual(event_risk_context(underlying)["event_risk_reason"], "fed_decision")
        self.assertIsNotNone(
            CashSecuredPutStrategy(config).generate_order(
                _context(config, {**underlying, "event_risk": False, "event_risk_reason": ""})
            )
        )

    def test_event_risk_level_filter_blocks_when_configured(self):
        config = deepcopy(load_config())
        config["strategies"]["cash_secured_put"]["max_event_risk_level"] = 0.5
        underlying = {
            "symbol": "SPY",
            "strategies_allowed": ["cash_secured_put"],
            "event_risk_level": "high",
        }

        self.assertIsNone(CashSecuredPutStrategy(config).generate_order(_context(config, underlying)))
        self.assertIsNotNone(
            CashSecuredPutStrategy(config).generate_order(
                _context(config, {**underlying, "event_risk_level": "low"})
            )
        )

    def test_event_risk_context_does_not_change_selection_score(self):
        config = load_config()
        order_without = StrategyOrder("cash_secured_put", "plain", "SPY", [], 100.0, 20.0, 100.0, 0.5, 2.0, None, 55.0)
        order_with = StrategyOrder("cash_secured_put", "event", "SPY", [], 100.0, 20.0, 100.0, 0.5, 2.0, None, 55.0)
        base_context = _context(config)
        event_context = replace(
            base_context,
            underlying={
                **base_context.underlying,
                "event_risk": True,
                "event_risk_level": "high",
            },
        )

        self.assertEqual(score_order(order_without, base_context, config), score_order(order_with, event_context, config))
        self.assertTrue(order_with.metadata["selection"]["event_risk"]["event_risk"])

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

    def test_realized_vs_implied_volatility_spread_filter_blocks_vertical(self):
        config = deepcopy(load_config())
        config["strategies"]["vertical_spread"]["enabled"] = True
        config["strategies"]["vertical_spread"]["min_credit_to_width"] = 0.0
        config["strategies"]["vertical_spread"]["min_net_credit"] = 0.0
        config["strategies"]["vertical_spread"]["min_iv_realized_spread"] = 0.05
        expiration = date(2026, 5, 28)
        chain = [
            OptionContract("SPY260528P00095000", "SPY", "put", 95.0, expiration, 1.2, 1.24, open_interest=500, volume=50, delta=-0.25, underlying_price=100.0),
            OptionContract("SPY260528P00090000", "SPY", "put", 90.0, expiration, 0.4, 0.42, open_interest=500, volume=50, delta=-0.10, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["vertical_spread"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=50.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.20,
            realized_vol_20d=0.18,
        )

        self.assertIsNone(VerticalSpreadStrategy(config).generate_order(context))
        richer_context = replace(context, current_iv=0.25)
        self.assertIsNotNone(VerticalSpreadStrategy(config).generate_order(richer_context))

    def test_skew_filter_blocks_cheap_put_tail_vertical(self):
        config = deepcopy(load_config())
        config["strategies"]["vertical_spread"]["enabled"] = True
        config["strategies"]["vertical_spread"]["min_credit_to_width"] = 0.0
        config["strategies"]["vertical_spread"]["min_net_credit"] = 0.0
        config["strategies"]["vertical_spread"]["min_put_tail_skew"] = 0.05
        expiration = date(2026, 5, 28)
        chain = [
            OptionContract("SPY260528C00100000", "SPY", "call", 100.0, expiration, 2.0, 2.04, open_interest=500, volume=50, implied_volatility=0.30, delta=0.50, underlying_price=100.0),
            OptionContract("SPY260528P00100000", "SPY", "put", 100.0, expiration, 2.0, 2.04, open_interest=500, volume=50, implied_volatility=0.30, delta=-0.50, underlying_price=100.0),
            OptionContract("SPY260528P00095000", "SPY", "put", 95.0, expiration, 1.2, 1.24, open_interest=500, volume=50, implied_volatility=0.31, delta=-0.25, underlying_price=100.0),
            OptionContract("SPY260528P00090000", "SPY", "put", 90.0, expiration, 0.4, 0.42, open_interest=500, volume=50, implied_volatility=0.29, delta=-0.10, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["vertical_spread"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=50.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.35,
            realized_vol_20d=0.20,
        )

        self.assertIsNone(VerticalSpreadStrategy(config).generate_order(context))
        richer_tail = [
            replace(contract, implied_volatility=0.38) if contract.contract_symbol == "SPY260528P00095000" else contract
            for contract in chain
        ]
        self.assertIsNotNone(VerticalSpreadStrategy(config).generate_order(replace(context, chain=richer_tail)))

    def test_term_structure_filter_blocks_disallowed_slope(self):
        config = deepcopy(load_config())
        config["strategies"]["vertical_spread"]["enabled"] = True
        config["strategies"]["vertical_spread"]["min_credit_to_width"] = 0.0
        config["strategies"]["vertical_spread"]["min_net_credit"] = 0.0
        config["strategies"]["vertical_spread"]["max_term_structure_slope"] = 0.05
        front = date(2026, 5, 28)
        back = date(2026, 6, 11)
        chain = [
            OptionContract("SPY260528C00100000", "SPY", "call", 100.0, front, 2.0, 2.04, open_interest=500, volume=50, implied_volatility=0.30, delta=0.50, underlying_price=100.0),
            OptionContract("SPY260528P00100000", "SPY", "put", 100.0, front, 2.0, 2.04, open_interest=500, volume=50, implied_volatility=0.30, delta=-0.50, underlying_price=100.0),
            OptionContract("SPY260528P00095000", "SPY", "put", 95.0, front, 1.2, 1.24, open_interest=500, volume=50, implied_volatility=0.38, delta=-0.25, underlying_price=100.0),
            OptionContract("SPY260528P00090000", "SPY", "put", 90.0, front, 0.4, 0.42, open_interest=500, volume=50, implied_volatility=0.35, delta=-0.10, underlying_price=100.0),
            OptionContract("SPY260611C00100000", "SPY", "call", 100.0, back, 3.0, 3.04, open_interest=500, volume=50, implied_volatility=0.42, delta=0.50, underlying_price=100.0),
            OptionContract("SPY260611P00100000", "SPY", "put", 100.0, back, 3.0, 3.04, open_interest=500, volume=50, implied_volatility=0.42, delta=-0.50, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["vertical_spread"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=50.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.35,
            realized_vol_20d=0.20,
        )

        self.assertIsNone(VerticalSpreadStrategy(config).generate_order(context))
        flatter_back = [
            replace(contract, implied_volatility=0.33) if contract.expiration == back else contract
            for contract in chain
        ]
        self.assertIsNotNone(VerticalSpreadStrategy(config).generate_order(replace(context, chain=flatter_back)))

    def test_broken_wing_butterfly_generates_defined_risk_credit_order(self):
        config = deepcopy(load_config())
        config["strategies"]["broken_wing_butterfly"]["enabled"] = True
        config["strategies"]["broken_wing_butterfly"]["min_net_credit"] = 1.0
        config["strategies"]["broken_wing_butterfly"]["min_credit_to_width"] = 0.0
        expiration = date(2026, 5, 28)
        chain = [
            OptionContract("SPY260528P00090000", "SPY", "put", 90.0, expiration, 0.2, 0.21, open_interest=500, volume=50, delta=-0.10, underlying_price=100.0),
            OptionContract("SPY260528P00095000", "SPY", "put", 95.0, expiration, 1.5, 1.55, open_interest=500, volume=50, delta=-0.25, underlying_price=100.0),
            OptionContract("SPY260528P00105000", "SPY", "put", 105.0, expiration, 2.0, 2.05, open_interest=500, volume=50, delta=-0.45, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["broken_wing_butterfly"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=70.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.35,
        )

        order = BrokenWingButterflyStrategy(config).generate_order(context)

        self.assertIsNotNone(order)
        self.assertEqual(order.strategy_name, "broken_wing_butterfly")
        self.assertEqual([leg.side for leg in order.legs], ["buy_to_open", "sell_to_open", "buy_to_open"])
        self.assertEqual([leg.qty for leg in order.legs], [1, 2, 1])
        self.assertGreater(order.net_opening_credit, 0)
        self.assertGreater(order.max_loss, 0)
        self.assertEqual(order.required_options_level, 3)
        decision = pre_trade_check(order, account=context.account, config=config, open_positions=[], as_of=context.as_of)
        self.assertTrue(decision.accepted)

    def test_broken_wing_butterfly_disabled_by_default(self):
        self.assertNotIn("broken_wing_butterfly", [strategy.name for strategy in build_enabled_strategies(load_config())])

    def test_long_butterfly_generates_defined_risk_debit_order(self):
        config = deepcopy(load_config())
        config["strategies"]["butterfly"]["enabled"] = True
        config["strategies"]["butterfly"]["variant"] = "long_debit"
        expiration = date(2026, 5, 28)
        chain = [
            OptionContract("SPY260528P00090000", "SPY", "put", 90.0, expiration, 0.5, 0.51, open_interest=500, volume=50, delta=-0.10, underlying_price=100.0),
            OptionContract("SPY260528P00095000", "SPY", "put", 95.0, expiration, 1.0, 1.02, open_interest=500, volume=50, delta=-0.25, underlying_price=100.0),
            OptionContract("SPY260528P00100000", "SPY", "put", 100.0, expiration, 2.0, 2.04, open_interest=500, volume=50, delta=-0.45, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["butterfly"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=35.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.25,
        )

        order = ButterflyStrategy(config).generate_order(context)

        self.assertIsNotNone(order)
        self.assertEqual([leg.side for leg in order.legs], ["buy_to_open", "sell_to_open", "buy_to_open"])
        self.assertEqual(order.legs[1].contract.strike - order.legs[0].contract.strike, order.legs[2].contract.strike - order.legs[1].contract.strike)
        self.assertLess(order.net_opening_credit, 0)
        self.assertGreater(order.max_profit, order.max_loss)
        decision = pre_trade_check(order, account=context.account, config=config, open_positions=[], as_of=context.as_of)
        self.assertTrue(decision.accepted)

    def test_short_butterfly_generates_defined_risk_credit_order(self):
        config = deepcopy(load_config())
        config["strategies"]["butterfly"]["enabled"] = True
        config["strategies"]["butterfly"]["variant"] = "short_credit"
        config["strategies"]["butterfly"]["min_credit_to_width"] = 0.0
        expiration = date(2026, 5, 28)
        chain = [
            OptionContract("SPY260528P00090000", "SPY", "put", 90.0, expiration, 1.0, 1.02, open_interest=500, volume=50, delta=-0.10, underlying_price=100.0),
            OptionContract("SPY260528P00095000", "SPY", "put", 95.0, expiration, 0.55, 0.56, open_interest=500, volume=50, delta=-0.25, underlying_price=100.0),
            OptionContract("SPY260528P00100000", "SPY", "put", 100.0, expiration, 1.0, 1.02, open_interest=500, volume=50, delta=-0.45, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["butterfly"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=60.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.35,
        )

        order = ButterflyStrategy(config).generate_order(context)

        self.assertIsNotNone(order)
        self.assertEqual([leg.side for leg in order.legs], ["sell_to_open", "buy_to_open", "sell_to_open"])
        self.assertEqual([leg.qty for leg in order.legs], [1, 2, 1])
        self.assertGreater(order.net_opening_credit, 0)
        self.assertGreater(order.max_loss, 0)
        decision = pre_trade_check(order, account=context.account, config=config, open_positions=[], as_of=context.as_of)
        self.assertTrue(decision.accepted)

    def test_butterfly_disabled_by_default(self):
        self.assertNotIn("butterfly", [strategy.name for strategy in build_enabled_strategies(load_config())])

    def test_collar_generates_defined_risk_order_for_long_stock(self):
        config = deepcopy(load_config())
        config["strategies"]["collar"]["enabled"] = True
        expiration = date(2026, 5, 28)
        chain = [
            OptionContract("SPY260528P00095000", "SPY", "put", 95.0, expiration, 1.0, 1.02, open_interest=500, volume=50, delta=-0.20, underlying_price=100.0),
            OptionContract("SPY260528C00105000", "SPY", "call", 105.0, expiration, 1.2, 1.22, open_interest=500, volume=50, delta=0.20, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["collar"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=60.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.30,
            long_shares=100,
            cost_basis=100.0,
        )

        order = CollarStrategy(config).generate_order(context)

        self.assertIsNotNone(order)
        self.assertEqual(order.strategy_name, "collar")
        self.assertEqual([leg.side for leg in order.legs], ["buy_to_open", "sell_to_open"])
        self.assertEqual([leg.qty for leg in order.legs], [1, 1])
        self.assertGreater(order.net_opening_credit, 0)
        self.assertEqual(order.metadata["covered_shares"], 100)
        self.assertGreater(order.max_loss, 0)
        self.assertGreater(order.max_profit, 0)
        decision = pre_trade_check(order, account=context.account, config=config, open_positions=[], as_of=context.as_of)
        self.assertTrue(decision.accepted)

    def test_collar_requires_long_stock_inventory(self):
        config = deepcopy(load_config())
        config["strategies"]["collar"]["enabled"] = True
        expiration = date(2026, 5, 28)
        chain = [
            OptionContract("SPY260528P00095000", "SPY", "put", 95.0, expiration, 1.0, 1.02, open_interest=500, volume=50, delta=-0.20, underlying_price=100.0),
            OptionContract("SPY260528C00105000", "SPY", "call", 105.0, expiration, 1.2, 1.22, open_interest=500, volume=50, delta=0.20, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["collar"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=60.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.30,
            long_shares=0,
            cost_basis=0.0,
        )

        self.assertIsNone(CollarStrategy(config).generate_order(context))

    def test_collar_disabled_by_default(self):
        self.assertNotIn("collar", [strategy.name for strategy in build_enabled_strategies(load_config())])

    def test_diagonal_spread_generates_covered_front_short_order(self):
        config = deepcopy(load_config())
        config["strategies"]["diagonal_spread"]["enabled"] = True
        chain = [
            OptionContract("SPY260514C00105000", "SPY", "call", 105.0, date(2026, 5, 14), 1.0, 1.02, open_interest=500, volume=50, delta=0.30, underlying_price=100.0),
            OptionContract("SPY260604C00100000", "SPY", "call", 100.0, date(2026, 6, 4), 3.0, 3.04, open_interest=500, volume=50, delta=0.45, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["diagonal_spread"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=30.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.25,
        )

        order = DiagonalSpreadStrategy(config).generate_order(context)

        self.assertIsNotNone(order)
        self.assertEqual(order.strategy_name, "diagonal_spread")
        self.assertEqual([leg.side for leg in order.legs], ["sell_to_open", "buy_to_open"])
        self.assertLess(order.legs[0].contract.expiration, order.legs[1].contract.expiration)
        self.assertLessEqual(order.legs[1].contract.strike, order.legs[0].contract.strike)
        self.assertLess(order.net_opening_credit, 0)
        self.assertEqual(order.max_loss, order.required_buying_power)
        decision = pre_trade_check(order, account=context.account, config=config, open_positions=[], as_of=context.as_of)
        self.assertTrue(decision.accepted)

    def test_diagonal_spread_rejects_uncovered_front_call(self):
        config = deepcopy(load_config())
        config["strategies"]["diagonal_spread"]["enabled"] = True
        chain = [
            OptionContract("SPY260514C00105000", "SPY", "call", 105.0, date(2026, 5, 14), 1.0, 1.02, open_interest=500, volume=50, delta=0.30, underlying_price=100.0),
            OptionContract("SPY260604C00110000", "SPY", "call", 110.0, date(2026, 6, 4), 3.0, 3.04, open_interest=500, volume=50, delta=0.45, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["diagonal_spread"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=30.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.25,
        )

        self.assertIsNone(DiagonalSpreadStrategy(config).generate_order(context))

    def test_diagonal_spread_assignment_risk_is_flagged(self):
        front = OptionContract(
            "SPY260514C00100000",
            "SPY",
            "call",
            100.0,
            date(2026, 5, 14),
            5.0,
            5.0,
            open_interest=500,
            volume=50,
            underlying_price=105.0,
        )
        back = OptionContract(
            "SPY260604C00095000",
            "SPY",
            "call",
            95.0,
            date(2026, 6, 4),
            11.0,
            11.0,
            open_interest=500,
            volume=50,
            underlying_price=105.0,
        )
        position = PositionSnapshot(
            strategy_id="diagonal-SPY-20260423",
            strategy_name="diagonal_spread",
            underlying="SPY",
            legs=[OrderLeg(contract=front, side="sell_to_open"), OrderLeg(contract=back, side="buy_to_open")],
            opened_at=datetime(2026, 4, 23),
            entry_credit=-600.0,
            max_loss=600.0,
            profit_take_pct=0.3,
            loss_stop_multiple=1.0,
            roll_threshold_delta=None,
        )

        result = continuous_risk_checks([position], config=load_config(), as_of=datetime(2026, 4, 23))

        self.assertIn({"strategy_id": "diagonal-SPY-20260423", "action": "close_for_calendar_assignment"}, result["actions"])

    def test_diagonal_spread_disabled_by_default(self):
        self.assertNotIn("diagonal_spread", [strategy.name for strategy in build_enabled_strategies(load_config())])

    def test_tail_risk_hedge_generates_budgeted_long_put_when_triggered(self):
        config = deepcopy(load_config())
        config["strategies"]["tail_risk_hedge"]["enabled"] = True
        expiration = date(2026, 6, 11)
        chain = [
            OptionContract("SPY260618P00085000", "SPY", "put", 85.0, expiration, 1.0, 1.02, open_interest=500, volume=50, delta=-0.10, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["tail_risk_hedge"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0, "drawdown_pct": 0.06},
            iv_rank=30.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.25,
            realized_vol_20d=0.20,
        )

        order = TailRiskHedgeStrategy(config).generate_order(context)

        self.assertIsNotNone(order)
        self.assertEqual(order.strategy_name, "tail_risk_hedge")
        self.assertEqual([leg.side for leg in order.legs], ["buy_to_open"])
        self.assertEqual(order.metadata["trigger"], "drawdown")
        self.assertLess(order.net_opening_credit, 0)
        self.assertEqual(order.max_loss, order.required_buying_power)
        decision = pre_trade_check(order, account=context.account, config=config, open_positions=[], as_of=context.as_of)
        self.assertTrue(decision.accepted)

    def test_tail_risk_hedge_requires_trigger_and_budget(self):
        config = deepcopy(load_config())
        config["strategies"]["tail_risk_hedge"]["enabled"] = True
        expiration = date(2026, 6, 11)
        chain = [
            OptionContract("SPY260618P00085000", "SPY", "put", 85.0, expiration, 1.0, 1.02, open_interest=500, volume=50, delta=-0.10, underlying_price=100.0),
        ]
        base_context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["tail_risk_hedge"], "max_contracts": 1},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=30.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.20,
            realized_vol_20d=0.20,
        )
        expensive_context = StrategyContext(
            underlying=base_context.underlying,
            chain=chain,
            config={**config, "strategies": {**config["strategies"], "tail_risk_hedge": {**config["strategies"]["tail_risk_hedge"], "premium_budget_pct": 0.001}}},
            account={**base_context.account, "drawdown_pct": 0.06},
            iv_rank=base_context.iv_rank,
            as_of=base_context.as_of,
            underlying_price=base_context.underlying_price,
            current_iv=base_context.current_iv,
            realized_vol_20d=base_context.realized_vol_20d,
        )

        self.assertIsNone(TailRiskHedgeStrategy(config).generate_order(base_context))
        self.assertIsNone(TailRiskHedgeStrategy(expensive_context.config).generate_order(expensive_context))

    def test_tail_risk_hedge_can_trigger_on_event_risk_flag(self):
        config = deepcopy(load_config())
        config["strategies"]["tail_risk_hedge"]["enabled"] = True
        expiration = date(2026, 6, 11)
        chain = [
            OptionContract("SPY260611P00085000", "SPY", "put", 85.0, expiration, 1.0, 1.02, open_interest=500, volume=50, delta=-0.10, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["tail_risk_hedge"], "event_risk": True},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=30.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.20,
            realized_vol_20d=0.20,
        )

        order = TailRiskHedgeStrategy(config).generate_order(context)

        self.assertIsNotNone(order)
        self.assertEqual(order.metadata["trigger"], "event_risk")

    def test_tail_risk_hedge_disabled_by_default(self):
        self.assertNotIn("tail_risk_hedge", [strategy.name for strategy in build_enabled_strategies(load_config())])

    def test_earnings_calendar_scanner_finds_filtered_research_candidate(self):
        config = deepcopy(load_config())
        params = deepcopy(config["research"]["earnings_calendar_spread"])
        params["enabled"] = True
        chain = [
            OptionContract("SPY260508C00105000", "SPY", "call", 105.0, date(2026, 5, 8), 2.0, 2.04, open_interest=700, volume=80, implied_volatility=0.70, delta=0.35, underlying_price=100.0),
            OptionContract("SPY260604C00105000", "SPY", "call", 105.0, date(2026, 6, 4), 3.0, 3.04, open_interest=800, volume=90, implied_volatility=0.40, delta=0.40, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["calendar_spread"], "next_earnings_date": "2026-04-30", "earnings_date_confidence": 0.95},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=65.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.60,
            next_earnings_date=date(2026, 4, 30),
        )

        candidates = scan_earnings_calendar_candidates(context, params)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["strategy"], "earnings_calendar_spread")
        self.assertEqual(candidates[0]["planned_exit_date"], "2026-05-01")
        self.assertGreaterEqual(candidates[0]["front_back_iv_spread"], params["min_front_back_iv_spread"])
        self.assertLessEqual(candidates[0]["debit"], params["max_debit"])

    def test_earnings_calendar_scanner_blocks_ex_dividend_proximity(self):
        config = deepcopy(load_config())
        params = deepcopy(config["research"]["earnings_calendar_spread"])
        params["enabled"] = True
        chain = [
            OptionContract("SPY260508C00105000", "SPY", "call", 105.0, date(2026, 5, 8), 2.0, 2.04, open_interest=700, volume=80, implied_volatility=0.70, delta=0.35, underlying_price=100.0),
            OptionContract("SPY260604C00105000", "SPY", "call", 105.0, date(2026, 6, 4), 3.0, 3.04, open_interest=800, volume=90, implied_volatility=0.40, delta=0.40, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={
                "symbol": "SPY",
                "strategies_allowed": ["calendar_spread"],
                "next_earnings_date": "2026-04-30",
                "earnings_date_confidence": 0.95,
                "ex_dividend_date": "2026-05-01",
            },
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=65.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.60,
            next_earnings_date=date(2026, 4, 30),
            ex_dividend_date=date(2026, 5, 1),
        )

        self.assertEqual(scan_earnings_calendar_candidates(context, params), [])

    def test_volatility_crush_iron_condor_research_candidate_filters_event_risk(self):
        config = deepcopy(load_config())
        params = deepcopy(config["research"]["volatility_crush_earnings_iron_condor"])
        params["enabled"] = True
        chain = [
            OptionContract("SPY260508P00090000", "SPY", "put", 90.0, date(2026, 5, 8), 1.5, 1.54, open_interest=700, volume=80, delta=-0.16, underlying_price=100.0),
            OptionContract("SPY260508P00085000", "SPY", "put", 85.0, date(2026, 5, 8), 0.5, 0.52, open_interest=700, volume=80, delta=-0.08, underlying_price=100.0),
            OptionContract("SPY260508C00110000", "SPY", "call", 110.0, date(2026, 5, 8), 1.4, 1.44, open_interest=700, volume=80, delta=0.16, underlying_price=100.0),
            OptionContract("SPY260508C00115000", "SPY", "call", 115.0, date(2026, 5, 8), 0.4, 0.42, open_interest=700, volume=80, delta=0.08, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["earnings_iron_condor"], "next_earnings_date": "2026-04-24"},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=80.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.30,
            next_earnings_date=date(2026, 4, 24),
        )

        candidates = scan_volatility_crush_iron_condor_candidates(context, params)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["strategy"], "volatility_crush_earnings_iron_condor")
        self.assertEqual(candidates[0]["planned_exit_date"], "2026-04-25")
        self.assertGreaterEqual(candidates[0]["credit"], params["min_credit"])
        self.assertLessEqual(candidates[0]["max_loss"], params["max_loss"])
        self.assertGreaterEqual(candidates[0]["short_call_strike"], 100.0 + candidates[0]["expected_move"])
        self.assertLessEqual(candidates[0]["short_put_strike"], 100.0 - candidates[0]["expected_move"])

    def test_volatility_crush_iron_condor_rejects_inside_expected_move(self):
        config = deepcopy(load_config())
        params = deepcopy(config["research"]["volatility_crush_earnings_iron_condor"])
        params["enabled"] = True
        chain = [
            OptionContract("SPY260508P00096000", "SPY", "put", 96.0, date(2026, 5, 8), 1.5, 1.54, open_interest=700, volume=80, delta=-0.16, underlying_price=100.0),
            OptionContract("SPY260508P00091000", "SPY", "put", 91.0, date(2026, 5, 8), 0.5, 0.52, open_interest=700, volume=80, delta=-0.08, underlying_price=100.0),
            OptionContract("SPY260508C00104000", "SPY", "call", 104.0, date(2026, 5, 8), 1.4, 1.44, open_interest=700, volume=80, delta=0.16, underlying_price=100.0),
            OptionContract("SPY260508C00109000", "SPY", "call", 109.0, date(2026, 5, 8), 0.4, 0.42, open_interest=700, volume=80, delta=0.08, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={"symbol": "SPY", "strategies_allowed": ["earnings_iron_condor"], "next_earnings_date": "2026-04-24"},
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=80.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.30,
            next_earnings_date=date(2026, 4, 24),
        )

        self.assertEqual(scan_volatility_crush_iron_condor_candidates(context, params), [])

    def test_volatility_crush_iron_condor_blocks_ex_dividend_proximity(self):
        config = deepcopy(load_config())
        params = deepcopy(config["research"]["volatility_crush_earnings_iron_condor"])
        params["enabled"] = True
        chain = [
            OptionContract("SPY260508P00090000", "SPY", "put", 90.0, date(2026, 5, 8), 1.5, 1.54, open_interest=700, volume=80, delta=-0.16, underlying_price=100.0),
            OptionContract("SPY260508P00085000", "SPY", "put", 85.0, date(2026, 5, 8), 0.5, 0.52, open_interest=700, volume=80, delta=-0.08, underlying_price=100.0),
            OptionContract("SPY260508C00110000", "SPY", "call", 110.0, date(2026, 5, 8), 1.4, 1.44, open_interest=700, volume=80, delta=0.16, underlying_price=100.0),
            OptionContract("SPY260508C00115000", "SPY", "call", 115.0, date(2026, 5, 8), 0.4, 0.42, open_interest=700, volume=80, delta=0.08, underlying_price=100.0),
        ]
        context = StrategyContext(
            underlying={
                "symbol": "SPY",
                "strategies_allowed": ["earnings_iron_condor"],
                "next_earnings_date": "2026-04-24",
                "ex_dividend_date": "2026-05-01",
            },
            chain=chain,
            config=config,
            account={"equity": 100000.0, "portfolio_value": 100000.0, "cash": 100000.0, "buying_power": 200000.0},
            iv_rank=80.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
            current_iv=0.30,
            next_earnings_date=date(2026, 4, 24),
            ex_dividend_date=date(2026, 5, 1),
        )

        self.assertEqual(scan_volatility_crush_iron_condor_candidates(context, params), [])

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
        self.assertEqual(result.provenance["data_source"], "fixture")
        self.assertEqual(result.provenance["option_chain_coverage"]["provider"], "json_replay")
        self.assertIn("SPY", result.provenance["symbols"])

    def test_historical_replay_loads_events_and_lifecycle_filtered_chain(self):
        payload = {
            "metadata": {"provider": "unit-test-provider"},
            "underlyings": {
                "SPY": {
                    "2026-04-20": {
                        "price": 100.0,
                        "current_iv": 0.30,
                        "iv_rank": 55.0,
                        "realized_vol_20d": 0.12,
                        "atr_pct": 0.01,
                    }
                }
            },
            "earnings": {"SPY": [{"date": "2026-04-25"}]},
            "dividends": {"SPY": [{"ex_dividend_date": "2026-04-24", "amount": 1.23}]},
            "corporate_actions": {"SPY": [{"date": "2026-04-20", "type": "split", "ratio": "2:1"}]},
            "chains": {
                "SPY": {
                    "2026-04-20": [
                        {
                            "contract_symbol": "SPY260525P00095000",
                            "option_type": "put",
                            "strike": 95.0,
                            "expiration": "2026-05-25",
                            "bid": 1.0,
                            "ask": 1.1,
                            "open_interest": 500,
                            "volume": 100,
                            "implied_volatility": 0.3,
                            "delta": -0.2,
                            "theta": -0.04,
                            "vega": 0.1,
                            "gamma": 0.01,
                            "underlying_price": 100.0,
                            "listed_date": "2026-04-01",
                            "quote_timestamp": "2026-04-20T15:59:00+00:00",
                        },
                        {
                            "contract_symbol": "SPY260525P00090000",
                            "option_type": "put",
                            "strike": 90.0,
                            "expiration": "2026-05-25",
                            "bid": 0.5,
                            "ask": 0.6,
                            "open_interest": 500,
                            "volume": 100,
                            "listed_date": "2026-04-21",
                        },
                    ]
                }
            },
        }
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "historical_replay.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            config = deepcopy(load_config())
            config["backtest"]["data_source"] = "historical_replay"
            config["backtest"]["historical_data_file"] = str(path)

            client = HistoricalReplayClient(config)
            snapshot = client.get_underlying_snapshot("SPY", as_of=date(2026, 4, 20))
            chain = client.get_option_chain("SPY", as_of=date(2026, 4, 20))
            coverage = client.coverage_summary()
            actions = client.corporate_actions_for("SPY", date(2026, 4, 20))

        self.assertEqual(snapshot["next_earnings_date"], "2026-04-25")
        self.assertEqual(snapshot["ex_dividend_date"], "2026-04-24")
        self.assertEqual(snapshot["dividend_amount"], 1.23)
        self.assertEqual(snapshot["iv_percentile"], 55.0)
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].meta["quote_timestamp"], "2026-04-20T15:59:00+00:00")
        self.assertEqual(actions[0]["type"], "split")
        self.assertEqual(coverage["provider"], "unit-test-provider")
        self.assertEqual(coverage["contract_count"], 2)

    def test_historical_replay_loads_csv_data_source(self):
        csv_text = "\n".join(
            [
                "record_type,symbol,date,price,current_iv,iv_rank,realized_vol_20d,atr_pct,contract_symbol,option_type,strike,expiration,bid,ask,open_interest,volume,delta,ex_dividend_date,amount",
                "snapshot,SPY,2026-04-20,100,0.30,55,0.12,0.01,,,,,,,,,,",
                "contract,SPY,2026-04-20,,,,,,SPY260525P00095000,put,95,2026-05-25,1.0,1.1,500,100,-0.2,,",
                "dividend,SPY,2026-04-24,,,,,,,,,,,,,,,,2026-04-24,1.23",
            ]
        )
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "historical_replay.csv"
            path.write_text(csv_text, encoding="utf-8")
            config = deepcopy(load_config())
            config["backtest"]["data_source"] = "historical_replay"
            config["backtest"]["historical_data_file"] = str(path)

            client = HistoricalReplayClient(config)
            snapshot = client.get_underlying_snapshot("SPY", as_of=date(2026, 4, 20))
            chain = client.get_option_chain("SPY", as_of=date(2026, 4, 20))

        self.assertEqual(snapshot["price"], "100")
        self.assertEqual(snapshot["ex_dividend_date"], "2026-04-24")
        self.assertEqual(len(chain), 1)
        self.assertEqual(chain[0].contract_symbol, "SPY260525P00095000")
        self.assertEqual(chain[0].delta, -0.2)

    def test_provider_backed_replay_is_explicit_boundary(self):
        config = deepcopy(load_config())
        config["backtest"]["data_source"] = "historical_replay"
        config["backtest"]["historical_data_file"] = "unused.provider"
        config["backtest"]["historical_data_format"] = "provider"
        config["backtest"]["historical_provider"] = "example_vendor"

        with self.assertRaisesRegex(NotImplementedError, "example_vendor"):
            HistoricalReplayClient(config)


if __name__ == "__main__":
    unittest.main()
