from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from core.models import OptionContract, OrderLeg, PositionSnapshot, StrategyOrder
from core.portfolio_beta import aggregate_spy_beta_delta, order_spy_beta_delta
from core.risk_manager import (
    continuous_risk_checks,
    identify_elevated_positions,
    pre_trade_check,
    write_greeks_snapshot,
)


def _contract(
    symbol: str,
    option_type: str = "put",
    delta: float = -0.2,
    bid: float = 1.0,
    ask: float = 1.05,
) -> OptionContract:
    return OptionContract(
        contract_symbol=symbol,
        underlying="SPY",
        option_type=option_type,
        strike=500.0,
        expiration=date(2026, 6, 1),
        bid=bid,
        ask=ask,
        open_interest=500,
        volume=50,
        implied_volatility=0.24,
        delta=delta,
        theta=-0.1,
        vega=0.2,
        gamma=0.01,
        underlying_price=520.0,
    )


def _order(max_loss: float = 100.0, iv_rank: float = 50.0) -> StrategyOrder:
    contract = _contract("SPY260619P00500000")
    return StrategyOrder(
        strategy_name="vertical_spread",
        strategy_id="vertical_spread-SPY-20260423",
        underlying="SPY",
        legs=[
            OrderLeg(contract=contract, side="sell_to_open"),
            OrderLeg(contract=_contract("SPY260619P00499000", delta=-0.18, bid=0.7, ask=0.75), side="buy_to_open"),
        ],
        max_loss=max_loss,
        max_profit=25.0,
        required_buying_power=max_loss,
        profit_take_pct=0.5,
        loss_stop_multiple=1.5,
        roll_threshold_delta=-0.4,
        iv_rank=iv_rank,
        required_options_level=3,
    )


class PreTradeRiskTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "mode": "paper",
            "account": {
                "options_level": 3,
                "pdt_threshold_usd": 25000,
                "max_portfolio_risk_pct": 0.2,
                "max_single_position_risk_pct": 0.05,
                "max_open_strategies": 8,
                "reserve_cash_pct": 0.15,
            },
            "gates": {
                "min_open_interest": 100,
                "min_daily_volume": 10,
                "max_bid_ask_spread_pct": 0.1,
                "min_dte_entry": 7,
                "max_dte_entry": 55,
                "earnings_blackout_days_before": 5,
                "close_positions_days_before_earnings": 2,
                "min_iv_rank_for_short_premium": 30,
                "max_iv_rank_for_long_premium": 40,
            },
            "schedule": {"expiration_exit_cutoff_time": "15:15"},
        }
        self.account = {"equity": 10000.0, "portfolio_value": 10000.0, "cash": 10000.0, "buying_power": 20000.0}

    def test_accepts_small_defined_risk_trade(self):
        decision = pre_trade_check(_order(), account=self.account, config=self.config, open_positions=[], as_of=date(2026, 4, 23))
        self.assertTrue(decision.accepted)

    def test_rejects_projected_spy_beta_delta_ceiling(self):
        config = {
            **self.config,
            "portfolio_beta_limits": {
                "enabled": True,
                "max_abs_spy_beta_delta_pct": 0.05,
                "symbol_betas": {"SPY": 1.0},
            },
        }

        decision = pre_trade_check(_order(), account=self.account, config=config, open_positions=[], as_of=date(2026, 4, 23))

        self.assertIn("portfolio_spy_beta_delta_limit_exceeded", decision.reasons)

    def test_spy_beta_delta_uses_configured_symbol_beta(self):
        config = {"portfolio_beta_limits": {"symbol_betas": {"SPY": 0.5}}}
        order = _order()

        self.assertEqual(order_spy_beta_delta(order, config), 520.0)
        self.assertEqual(aggregate_spy_beta_delta([], config), 0.0)

    def test_spy_beta_delta_honors_negative_configured_symbol_beta(self):
        config = {"portfolio_beta_limits": {"symbol_betas": {"SPY": -0.5}}}

        self.assertEqual(order_spy_beta_delta(_order(), config), -520.0)

    def test_rejects_earnings_blackout(self):
        order = _order()
        order.next_earnings_date = date(2026, 4, 25)
        decision = pre_trade_check(order, account=self.account, config=self.config, open_positions=[], as_of=date(2026, 4, 23))
        self.assertIn("earnings_blackout", decision.reasons)

    def test_rejects_low_iv_rank_for_short_premium(self):
        decision = pre_trade_check(_order(iv_rank=10.0), account=self.account, config=self.config, open_positions=[], as_of=date(2026, 4, 23))
        self.assertIn("iv_rank_too_low_for_short_premium", decision.reasons)

    def test_vix_scaling_raises_short_premium_iv_threshold_when_vix_is_low(self):
        config = {
            **self.config,
            "gates": {
                **self.config["gates"],
                "vix_iv_rank_scaling": {
                    "enabled": True,
                    "low_vix_below": 15,
                    "low_vix_min_iv_rank_for_short_premium": 50,
                },
            },
        }
        account = {**self.account, "market_context": {"vix": 12.0}}

        decision = pre_trade_check(_order(iv_rank=45.0), account=account, config=config, open_positions=[], as_of=date(2026, 4, 23))

        self.assertIn("iv_rank_too_low_for_short_premium", decision.reasons)

    def test_vix_scaling_uses_high_vix_threshold_when_vix_is_elevated(self):
        config = {
            **self.config,
            "gates": {
                **self.config["gates"],
                "vix_iv_rank_scaling": {
                    "enabled": True,
                    "high_vix_above": 25,
                    "high_vix_min_iv_rank_for_short_premium": 35,
                },
            },
        }
        account = {**self.account, "market_context": {"vix": 30.0}}

        accepted = pre_trade_check(_order(iv_rank=36.0), account=account, config=config, open_positions=[], as_of=date(2026, 4, 23))
        rejected = pre_trade_check(_order(iv_rank=34.0), account=account, config=config, open_positions=[], as_of=date(2026, 4, 23))

        self.assertNotIn("iv_rank_too_low_for_short_premium", accepted.reasons)
        self.assertIn("iv_rank_too_low_for_short_premium", rejected.reasons)

    def test_vix_scaling_fails_closed_without_market_context(self):
        config = {
            **self.config,
            "gates": {
                **self.config["gates"],
                "vix_iv_rank_scaling": {
                    "enabled": True,
                    "low_vix_below": 15,
                    "low_vix_min_iv_rank_for_short_premium": 50,
                },
            },
        }

        decision = pre_trade_check(_order(iv_rank=60.0), account=self.account, config=config, open_positions=[], as_of=date(2026, 4, 23))

        self.assertIn("vix_unavailable_for_iv_rank_scaling", decision.reasons)

    def test_rejects_low_iv_rank_for_credit_butterfly_variant(self):
        order = _order(iv_rank=10.0)
        order.strategy_name = "butterfly"

        decision = pre_trade_check(order, account=self.account, config=self.config, open_positions=[], as_of=date(2026, 4, 23))

        self.assertIn("iv_rank_too_low_for_short_premium", decision.reasons)

    def test_rejects_high_iv_rank_for_debit_butterfly_variant(self):
        order = _order(iv_rank=80.0)
        order.strategy_name = "butterfly"
        order.legs = [
            OrderLeg(contract=_contract("SPY260619P00500000", bid=0.7, ask=0.75), side="sell_to_open"),
            OrderLeg(contract=_contract("SPY260619P00499000", delta=-0.18, bid=1.0, ask=1.05), side="buy_to_open"),
        ]

        decision = pre_trade_check(order, account=self.account, config=self.config, open_positions=[], as_of=date(2026, 4, 23))

        self.assertIn("iv_rank_too_high_for_long_premium", decision.reasons)

    def test_rejects_dte_too_short(self):
        order = _order()
        # Replace contracts with same-day expiration.
        soon = date(2026, 4, 23)
        for leg in order.legs:
            object.__setattr__(leg.contract, "expiration", soon)
        decision = pre_trade_check(
            order,
            account=self.account,
            config=self.config,
            open_positions=[],
            as_of=soon,
        )
        self.assertIn("dte_gate_failed", decision.reasons)

    def test_rejects_dte_too_long(self):
        order = _order()
        far = date(2027, 4, 23)
        for leg in order.legs:
            object.__setattr__(leg.contract, "expiration", far)
        decision = pre_trade_check(
            order,
            account=self.account,
            config=self.config,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )
        self.assertIn("dte_gate_failed", decision.reasons)

    def test_rejects_liquidity_failure(self):
        order = _order()
        for leg in order.legs:
            object.__setattr__(leg.contract, "open_interest", 0)
            object.__setattr__(leg.contract, "volume", 0)
        decision = pre_trade_check(
            order,
            account=self.account,
            config=self.config,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )
        self.assertIn("liquidity_gate_failed", decision.reasons)

    def test_rejects_stale_quote_timestamp(self):
        cfg = {
            **self.config,
            "gates": {
                **self.config["gates"],
                "max_quote_age_seconds": 60,
            },
        }
        order = _order()
        for leg in order.legs:
            leg.contract.meta["quote_timestamp"] = "2026-04-23T10:00:00+00:00"

        decision = pre_trade_check(
            order,
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=datetime(2026, 4, 23, 10, 2, tzinfo=timezone.utc),
        )

        self.assertIn("stale_quote", decision.reasons)

    def test_rejects_missing_quote_timestamp_when_required(self):
        cfg = {
            **self.config,
            "gates": {
                **self.config["gates"],
                "max_quote_age_seconds": 60,
                "reject_missing_quote_timestamp": True,
            },
        }

        decision = pre_trade_check(
            _order(),
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
        )

        self.assertIn("missing_quote_timestamp", decision.reasons)

    def test_accepts_recent_quote_timestamp_when_freshness_enabled(self):
        cfg = {
            **self.config,
            "gates": {
                **self.config["gates"],
                "max_quote_age_seconds": 60,
            },
        }
        order = _order()
        for leg in order.legs:
            leg.contract.meta["quote_timestamp"] = "2026-04-23T10:00:00+00:00"

        decision = pre_trade_check(
            order,
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=datetime(2026, 4, 23, 10, 0, 30, tzinfo=timezone.utc),
        )

        self.assertTrue(decision.accepted, decision.reasons)

    def test_quote_freshness_requires_datetime_when_max_age_enabled(self):
        cfg = {
            **self.config,
            "gates": {
                **self.config["gates"],
                "max_quote_age_seconds": 60,
            },
        }
        order = _order()
        for leg in order.legs:
            leg.contract.meta["quote_timestamp"] = "2026-04-23T10:00:00+00:00"

        decision = pre_trade_check(
            order,
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("quote_freshness_requires_datetime", decision.reasons)

    def test_datetime_as_of_supports_quote_freshness_and_earnings_blackout(self):
        cfg = {
            **self.config,
            "gates": {
                **self.config["gates"],
                "max_quote_age_seconds": 60,
            },
        }
        order = _order()
        order.next_earnings_date = date(2026, 4, 24)
        for leg in order.legs:
            leg.contract.meta["quote_timestamp"] = "2026-04-23T10:00:00+00:00"

        decision = pre_trade_check(
            order,
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=datetime(2026, 4, 23, 10, 0, 30, tzinfo=timezone.utc),
        )

        self.assertIn("earnings_blackout", decision.reasons)
        self.assertNotIn("stale_quote", decision.reasons)

    def test_rejects_wide_quote_with_specific_reason(self):
        order = _order()
        object.__setattr__(order.legs[0].contract, "bid", 1.0)
        object.__setattr__(order.legs[0].contract, "ask", 1.5)

        decision = pre_trade_check(
            order,
            account=self.account,
            config=self.config,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("liquidity_gate_failed", decision.reasons)
        self.assertIn("quote_spread_too_wide", decision.reasons)

    def test_rejects_portfolio_risk_cap(self):
        # Existing 1900 of risk leaves 100 of headroom; new order at
        # 200 should exceed.
        existing = PositionSnapshot(
            strategy_id="existing",
            strategy_name="vertical_spread",
            underlying="QQQ",
            legs=[OrderLeg(contract=_contract("QQQ260619P00500000"), side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=50.0,
            max_loss=1900.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
        )
        decision = pre_trade_check(
            _order(max_loss=200.0),
            account=self.account,
            config=self.config,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )
        self.assertIn("portfolio_risk_cap_exceeded", decision.reasons)

    def test_rejects_single_position_risk_cap(self):
        # 6% of 10k equity = 600; cap is 5%.
        decision = pre_trade_check(
            _order(max_loss=600.0),
            account=self.account,
            config=self.config,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )
        self.assertIn("single_position_risk_cap_exceeded", decision.reasons)

    def test_rejects_max_open_strategies(self):
        cfg = {**self.config, "account": {**self.config["account"], "max_open_strategies": 1}}
        existing = PositionSnapshot(
            strategy_id="existing",
            strategy_name="vertical_spread",
            underlying="QQQ",
            legs=[OrderLeg(contract=_contract("QQQ260619P00500000"), side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=50.0,
            max_loss=100.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
        )
        decision = pre_trade_check(
            _order(),
            account=self.account,
            config=cfg,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )
        self.assertIn("max_open_strategies_reached", decision.reasons)

    def test_rejects_options_level_too_low(self):
        cfg = {**self.config, "account": {**self.config["account"], "options_level": 1}}
        decision = pre_trade_check(
            _order(),
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )
        self.assertIn("options_level_too_low", decision.reasons)

    def test_rejects_invalid_mode(self):
        cfg = {**self.config, "mode": "wild_west"}
        decision = pre_trade_check(
            _order(),
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )
        self.assertIn("invalid_mode", decision.reasons)

    def test_rejects_ai_veto(self):
        order = _order()
        order.ai_veto_reason = "trade_critic_major_concern"
        decision = pre_trade_check(
            order,
            account=self.account,
            config=self.config,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )
        self.assertIn("ai_veto", decision.reasons)

    def test_rejects_conflicting_position(self):
        existing_contract = _contract("SPY260619P00500000")
        existing = PositionSnapshot(
            strategy_id="existing",
            strategy_name="vertical_spread",
            underlying="SPY",
            legs=[OrderLeg(contract=existing_contract, side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=50.0,
            max_loss=100.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
        )
        decision = pre_trade_check(
            _order(),
            account=self.account,
            config=self.config,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )
        self.assertIn("conflicting_position_exists", decision.reasons)

    def test_rejects_strategy_family_allocation_cap(self):
        cfg = {
            **self.config,
            "portfolio_allocation": {"family_caps_pct": {"short_premium": 0.15}},
        }
        existing = PositionSnapshot(
            strategy_id="existing",
            strategy_name="iron_condor",
            underlying="QQQ",
            legs=[OrderLeg(contract=_contract("QQQ260619P00500000"), side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=50.0,
            max_loss=1400.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
        )

        decision = pre_trade_check(
            _order(max_loss=200.0),
            account=self.account,
            config=cfg,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("portfolio_allocation_short_premium_cap_exceeded", decision.reasons)

    def test_rejects_single_underlying_allocation_cap(self):
        cfg = {
            **self.config,
            "portfolio_allocation": {"max_single_underlying_allocation_pct": 0.10},
        }
        existing_contract = _contract("SPY260619P00450000")
        object.__setattr__(existing_contract, "strike", 450.0)
        existing = PositionSnapshot(
            strategy_id="existing",
            strategy_name="vertical_spread",
            underlying="SPY",
            legs=[OrderLeg(contract=existing_contract, side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=50.0,
            max_loss=900.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
        )

        decision = pre_trade_check(
            _order(max_loss=200.0),
            account=self.account,
            config=cfg,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("underlying_allocation_cap_exceeded", decision.reasons)

    def test_ignores_invalid_allocation_caps(self):
        cfg = {
            **self.config,
            "portfolio_allocation": {
                "family_caps_pct": {"short_premium": "bad", "long_premium": -0.1},
                "underlying_caps_pct": {"SPY": ""},
            },
        }

        decision = pre_trade_check(
            _order(max_loss=200.0),
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertTrue(decision.accepted)

    def test_rejects_portfolio_delta_ceiling(self):
        cfg = {**self.config, "portfolio_greek_limits": {"delta": 1.0}}

        decision = pre_trade_check(
            _order(max_loss=100.0),
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("portfolio_delta_limit_exceeded", decision.reasons)

    def test_rejects_projected_portfolio_vega_ceiling(self):
        cfg = {**self.config, "portfolio_greek_limits": {"vega": 10.0}}
        existing = PositionSnapshot(
            strategy_id="existing",
            strategy_name="vertical_spread",
            underlying="QQQ",
            legs=[OrderLeg(contract=_contract("QQQ260619P00500000"), side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=50.0,
            max_loss=100.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
        )

        decision = pre_trade_check(
            _order(max_loss=100.0),
            account=self.account,
            config=cfg,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("portfolio_vega_limit_exceeded", decision.reasons)

    def test_ignores_invalid_portfolio_greek_limits(self):
        cfg = {**self.config, "portfolio_greek_limits": {"delta": "bad", "theta": -1.0}}

        decision = pre_trade_check(
            _order(max_loss=100.0),
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertTrue(decision.accepted)

    def test_rejects_sector_concentration_cap(self):
        cfg = {
            **self.config,
            "_underlying_metadata": {
                "SPY": {"sector": "technology"},
                "QQQ": {"sector": "technology"},
            },
            "portfolio_concentration": {"max_sector_allocation_pct": 0.10},
        }
        existing = PositionSnapshot(
            strategy_id="existing",
            strategy_name="vertical_spread",
            underlying="QQQ",
            legs=[OrderLeg(contract=_contract("QQQ260619P00500000"), side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=50.0,
            max_loss=900.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
        )

        decision = pre_trade_check(
            _order(max_loss=200.0),
            account=self.account,
            config=cfg,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("sector_concentration_cap_exceeded", decision.reasons)

    def test_sector_concentration_cap_normalizes_metadata_case(self):
        cfg = {
            **self.config,
            "_underlying_metadata": {
                "QQQ": {"sector": "technology"},
            },
            "portfolio_concentration": {"sector_caps_pct": {"Technology": 0.10}},
        }
        existing = PositionSnapshot(
            strategy_id="existing",
            strategy_name="vertical_spread",
            underlying="QQQ",
            legs=[OrderLeg(contract=_contract("QQQ260619P00500000"), side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=50.0,
            max_loss=900.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
        )
        order = _order(max_loss=200.0)
        order.metadata["underlying"] = {"sector": "Technology"}

        decision = pre_trade_check(
            order,
            account=self.account,
            config=cfg,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("sector_concentration_cap_exceeded", decision.reasons)

    def test_rejects_correlation_group_concentration_cap(self):
        cfg = {
            **self.config,
            "_underlying_metadata": {
                "SPY": {"correlation_group": "Broad_Index"},
                "IWM": {"correlation_group": "Broad_Index"},
            },
            "portfolio_concentration": {"correlation_group_caps_pct": {"broad_index": 0.10}},
        }
        existing = PositionSnapshot(
            strategy_id="existing",
            strategy_name="iron_condor",
            underlying="IWM",
            legs=[OrderLeg(contract=_contract("IWM260619P00500000"), side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=50.0,
            max_loss=900.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
        )

        decision = pre_trade_check(
            _order(max_loss=200.0),
            account=self.account,
            config=cfg,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("correlation_group_concentration_cap_exceeded", decision.reasons)

    def test_correlation_group_concentration_cap_normalizes_metadata_case(self):
        cfg = {
            **self.config,
            "_underlying_metadata": {
                "IWM": {"correlation_group": "broad_index"},
            },
            "portfolio_concentration": {"correlation_group_caps_pct": {"Broad_Index": 0.10}},
        }
        existing = PositionSnapshot(
            strategy_id="existing",
            strategy_name="iron_condor",
            underlying="IWM",
            legs=[OrderLeg(contract=_contract("IWM260619P00500000"), side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=50.0,
            max_loss=900.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
        )
        order = _order(max_loss=200.0)
        order.metadata["underlying"] = {"correlation_group": "Broad_Index"}

        decision = pre_trade_check(
            order,
            account=self.account,
            config=cfg,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("correlation_group_concentration_cap_exceeded", decision.reasons)

    def test_concentration_caps_ignore_missing_metadata(self):
        cfg = {
            **self.config,
            "portfolio_concentration": {
                "max_sector_allocation_pct": 0.0,
                "max_correlation_group_allocation_pct": 0.0,
            },
        }

        decision = pre_trade_check(
            _order(max_loss=100.0),
            account=self.account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertTrue(decision.accepted)

    def test_rejects_drawdown_halt_new_entries(self):
        cfg = {**self.config, "risk_throttle": {"max_drawdown_halt_pct": 0.10}}
        account = {**self.account, "peak_equity": 12000.0, "equity": 10000.0}

        decision = pre_trade_check(
            _order(max_loss=100.0),
            account=account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("drawdown_halt_new_entries", decision.reasons)

    def test_rejects_daily_loss_halt_new_entries(self):
        cfg = {**self.config, "risk_throttle": {"daily_loss_halt_pct": 0.03}}
        account = {**self.account, "daily_loss_pct": 0.04}

        decision = pre_trade_check(
            _order(max_loss=100.0),
            account=account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("daily_loss_halt_new_entries", decision.reasons)

    def test_risk_throttle_normalizes_percent_point_account_inputs(self):
        cfg = {
            **self.config,
            "risk_throttle": {
                "max_drawdown_halt_pct": 0.10,
                "daily_loss_halt_pct": 0.10,
            },
        }
        account = {**self.account, "drawdown_pct": 6, "daily_loss_pct": 6}

        decision = pre_trade_check(
            _order(max_loss=100.0),
            account=account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertTrue(decision.accepted)

    def test_rejects_drawdown_reduced_risk_size(self):
        cfg = {
            **self.config,
            "risk_throttle": {
                "reduce_risk_drawdown_pct": 0.05,
                "max_throttled_position_risk_pct": 0.01,
            },
        }
        account = {**self.account, "drawdown_pct": 0.06}

        decision = pre_trade_check(
            _order(max_loss=200.0),
            account=account,
            config=cfg,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertIn("drawdown_risk_throttle_exceeded", decision.reasons)

    def test_risk_throttle_defaults_to_noop(self):
        decision = pre_trade_check(
            _order(max_loss=100.0),
            account={**self.account, "drawdown_pct": 0.99, "daily_loss_pct": 0.99},
            config=self.config,
            open_positions=[],
            as_of=date(2026, 4, 23),
        )

        self.assertTrue(decision.accepted)


class CashSecuredPutPortfolioCashTests(unittest.TestCase):
    """Item 5: enforce that CSP entries respect post-assignment cash.

    A CSP's *position* max-loss is correctly bounded at strike*100 -
    credit, but the portfolio risk cap is what guarantees we have
    enough cash if multiple ITM puts assign at once.
    """

    def setUp(self):
        # Equity 30k, max_portfolio_risk_pct 0.20 -> cap = 6000.
        self.config = {
            "mode": "paper",
            "account": {
                "options_level": 3,
                "pdt_threshold_usd": 25000,
                "max_portfolio_risk_pct": 0.20,
                "max_single_position_risk_pct": 0.30,  # high so we isolate the portfolio cap
                "max_open_strategies": 8,
                "reserve_cash_pct": 0.0,
            },
            "gates": {
                "min_open_interest": 100,
                "min_daily_volume": 10,
                "max_bid_ask_spread_pct": 0.1,
                "min_dte_entry": 7,
                "max_dte_entry": 55,
                "earnings_blackout_days_before": 5,
                "close_positions_days_before_earnings": 2,
                "min_iv_rank_for_short_premium": 30,
                "max_iv_rank_for_long_premium": 40,
            },
            "schedule": {"expiration_exit_cutoff_time": "15:15"},
        }
        self.account = {
            "equity": 30000.0,
            "portfolio_value": 30000.0,
            "cash": 30000.0,
            "buying_power": 30000.0,
        }

    def _csp_position(self, symbol_suffix: str, max_loss: float) -> PositionSnapshot:
        contract = _contract(f"AAA260619P00100{symbol_suffix}")
        position = PositionSnapshot(
            strategy_id=f"csp-{symbol_suffix}",
            strategy_name="cash_secured_put",
            underlying=f"AAA{symbol_suffix}",
            legs=[OrderLeg(contract=contract, side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=80.0,
            max_loss=max_loss,
            profit_take_pct=0.5,
            loss_stop_multiple=2.0,
            roll_threshold_delta=-0.4,
            short_leg_itm=True,
        )
        return position

    def _new_csp_order(self, max_loss: float) -> StrategyOrder:
        contract = _contract("ZZZ260619P00100000")
        order = StrategyOrder(
            strategy_name="cash_secured_put",
            strategy_id="csp-new",
            underlying="ZZZ",
            legs=[OrderLeg(contract=contract, side="sell_to_open")],
            max_loss=max_loss,
            max_profit=80.0,
            required_buying_power=max_loss,
            profit_take_pct=0.5,
            loss_stop_multiple=2.0,
            roll_threshold_delta=-0.4,
            iv_rank=45.0,
            required_options_level=1,
        )
        return order

    def test_blocks_new_csp_when_existing_underwater_puts_consume_risk_cap(self):
        # Two existing CSPs each carrying 2500 of risk = 5000 used.
        # Cap is 6000 -> only 1000 of headroom. New CSP at 2000 must
        # be blocked.
        existing_a = self._csp_position("A", 2500.0)
        existing_b = self._csp_position("B", 2500.0)
        decision = pre_trade_check(
            self._new_csp_order(2000.0),
            account=self.account,
            config=self.config,
            open_positions=[existing_a, existing_b],
            as_of=date(2026, 4, 23),
        )
        self.assertIn("portfolio_risk_cap_exceeded", decision.reasons)

    def test_allows_new_csp_within_remaining_risk_budget(self):
        existing = self._csp_position("A", 2500.0)
        decision = pre_trade_check(
            self._new_csp_order(1000.0),
            account=self.account,
            config=self.config,
            open_positions=[existing],
            as_of=date(2026, 4, 23),
        )
        self.assertNotIn("portfolio_risk_cap_exceeded", decision.reasons)


class ContinuousRiskTests(unittest.TestCase):
    def test_flags_take_profit_and_roll_review(self):
        position = PositionSnapshot(
            strategy_id="spread-1",
            strategy_name="vertical_spread",
            underlying="SPY",
            legs=[OrderLeg(contract=_contract("SPY260619P00500000", delta=-0.45), side="sell_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=100.0,
            max_loss=200.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
            current_close_cost=40.0,
            current_pnl=60.0,
        )
        payload = continuous_risk_checks([position], config={"gates": {"close_positions_days_before_earnings": 2}, "schedule": {"expiration_exit_cutoff_time": "15:15"}}, as_of=datetime(2026, 4, 23, tzinfo=timezone.utc))
        actions = {item["action"] for item in payload["actions"]}
        self.assertIn("take_profit", actions)
        self.assertIn("roll_review", actions)

    def test_time_exit_uses_simulated_as_of_date(self):
        current_day = date.today()
        position = PositionSnapshot(
            strategy_id="spread-2",
            strategy_name="vertical_spread",
            underlying="SPY",
            legs=[
                OrderLeg(
                    contract=OptionContract(
                        contract_symbol="SPYTEST",
                        underlying="SPY",
                        option_type="put",
                        strike=500.0,
                        expiration=current_day + timedelta(days=10),
                        bid=1.0,
                        ask=1.05,
                        open_interest=500,
                        volume=50,
                        implied_volatility=0.24,
                        delta=-0.2,
                        theta=-0.1,
                        vega=0.2,
                        gamma=0.01,
                        underlying_price=520.0,
                    ),
                    side="sell_to_open",
                )
            ],
            opened_at=datetime.now(timezone.utc),
            entry_credit=100.0,
            max_loss=200.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
            current_close_cost=90.0,
            current_pnl=10.0,
        )
        simulated_as_of = datetime.combine(current_day - timedelta(days=20), datetime.min.time(), tzinfo=timezone.utc)
        payload = continuous_risk_checks(
            [position],
            config={"gates": {"close_positions_days_before_earnings": 2}, "schedule": {"expiration_exit_cutoff_time": "15:15"}},
            as_of=simulated_as_of,
        )
        actions = {item["action"] for item in payload["actions"]}
        self.assertNotIn("time_exit", actions)

    def test_debit_position_loss_alert_not_always_tripped(self):
        """Debit (long premium) positions must not trigger loss_alert when healthy."""
        position = PositionSnapshot(
            strategy_id="cal-1",
            strategy_name="calendar_spread",
            underlying="SPY",
            legs=[OrderLeg(contract=_contract("SPY260619P00500000", delta=-0.2), side="buy_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=-100.0,  # debit trade: negative entry_credit
            max_loss=100.0,
            profit_take_pct=0.5,
            loss_stop_multiple=2.0,
            roll_threshold_delta=None,
            current_close_cost=50.0,  # well within stop (2x * 100 = 200)
            current_pnl=-30.0,
        )
        elevated = identify_elevated_positions(
            [position],
            config={"gates": {"close_positions_days_before_earnings": 2}, "schedule": {"expiration_exit_cutoff_time": "15:15"}},
            as_of=datetime(2026, 4, 23, tzinfo=timezone.utc),
        )
        self.assertNotIn("cal-1", elevated)

    def test_debit_position_stop_loss_fires_when_breached(self):
        """Debit positions must trigger stop_loss when current_close_cost < entry debit (loss scenario)."""
        position = PositionSnapshot(
            strategy_id="cal-2",
            strategy_name="calendar_spread",
            underlying="SPY",
            legs=[OrderLeg(contract=_contract("SPY260619P00500000", delta=-0.2), side="buy_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=-100.0,
            max_loss=100.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=None,
            current_close_cost=200.0,  # exceeds abs(entry_credit) * loss_stop_multiple (100 * 1.5 = 150)
            current_pnl=-200.0,
        )
        payload = continuous_risk_checks(
            [position],
            config={"gates": {"close_positions_days_before_earnings": 2}, "schedule": {"expiration_exit_cutoff_time": "15:15"}},
            as_of=datetime(2026, 4, 23, tzinfo=timezone.utc),
        )
        actions = {item["action"] for item in payload["actions"]}
        self.assertIn("stop_loss", actions)

    def test_debit_position_take_profit_not_inverted(self):
        """Debit positions must not fire take_profit for a P&L of zero or negative."""
        position = PositionSnapshot(
            strategy_id="cal-3",
            strategy_name="calendar_spread",
            underlying="SPY",
            legs=[OrderLeg(contract=_contract("SPY260619P00500000", delta=-0.2), side="buy_to_open")],
            opened_at=datetime.now(timezone.utc),
            entry_credit=-100.0,
            max_loss=100.0,
            profit_take_pct=0.5,
            loss_stop_multiple=2.0,
            roll_threshold_delta=None,
            current_close_cost=80.0,
            current_pnl=-10.0,
        )
        payload = continuous_risk_checks(
            [position],
            config={"gates": {"close_positions_days_before_earnings": 2}, "schedule": {"expiration_exit_cutoff_time": "15:15"}},
            as_of=datetime(2026, 4, 23, tzinfo=timezone.utc),
        )
        actions = {item["action"] for item in payload["actions"]}
        self.assertNotIn("take_profit", actions)


class GreeksSnapshotTests(unittest.TestCase):
    def test_snapshot_filenames_include_microseconds(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)
            first = write_greeks_snapshot(
                directory,
                {"sequence": 1},
                as_of=datetime(2026, 4, 23, 16, 0, 0, 111111, tzinfo=timezone.utc),
            )
            second = write_greeks_snapshot(
                directory,
                {"sequence": 2},
                as_of=datetime(2026, 4, 23, 16, 0, 0, 222222, tzinfo=timezone.utc),
            )

            self.assertNotEqual(first, second)
            self.assertEqual(first.name, "20260423-160000-111111.json")
            self.assertEqual(second.name, "20260423-160000-222222.json")
            self.assertTrue(first.exists())
            self.assertTrue(second.exists())

    def test_snapshot_write_does_not_leave_per_file_lock(self):
        with TemporaryDirectory() as tmp:
            directory = Path(tmp)

            path = write_greeks_snapshot(
                directory,
                {"sequence": 1},
                as_of=datetime(2026, 4, 23, 16, 0, 0, 111111, tzinfo=timezone.utc),
            )

            self.assertTrue(path.exists())
            self.assertFalse(path.with_suffix(path.suffix + ".lock").exists())


if __name__ == "__main__":
    unittest.main()
