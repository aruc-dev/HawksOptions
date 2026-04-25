from __future__ import annotations

import unittest
from datetime import date, datetime, timezone

from core.models import OptionContract, OrderLeg, PositionSnapshot, StrategyOrder
from core.risk_manager import continuous_risk_checks, pre_trade_check


def _contract(symbol: str, option_type: str = "put", delta: float = -0.2) -> OptionContract:
    return OptionContract(
        contract_symbol=symbol,
        underlying="SPY",
        option_type=option_type,
        strike=500.0,
        expiration=date(2026, 6, 1),
        bid=1.0,
        ask=1.05,
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
        legs=[OrderLeg(contract=contract, side="sell_to_open"), OrderLeg(contract=_contract("SPY260619P00499000", delta=-0.18), side="buy_to_open")],
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

    def test_rejects_earnings_blackout(self):
        order = _order()
        order.next_earnings_date = date(2026, 4, 25)
        decision = pre_trade_check(order, account=self.account, config=self.config, open_positions=[], as_of=date(2026, 4, 23))
        self.assertIn("earnings_blackout", decision.reasons)

    def test_rejects_low_iv_rank_for_short_premium(self):
        decision = pre_trade_check(_order(iv_rank=10.0), account=self.account, config=self.config, open_positions=[], as_of=date(2026, 4, 23))
        self.assertIn("iv_rank_too_low_for_short_premium", decision.reasons)


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


if __name__ == "__main__":
    unittest.main()
