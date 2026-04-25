"""Tests for the structural trade critic."""

from __future__ import annotations

import unittest
from datetime import date

from ai.trade_idea_critic import critique_trade
from core.models import OptionContract, OrderLeg, StrategyOrder


def _contract(option_type: str, strike: float, *, mid: float = 1.0, theta: float = -0.05, vega: float = 0.10, spot: float = 100.0) -> OptionContract:
    return OptionContract(
        contract_symbol=f"XYZ260619{option_type[0].upper()}{int(strike*1000):08d}",
        underlying="XYZ",
        option_type=option_type,
        strike=strike,
        expiration=date(2026, 6, 19),
        bid=mid - 0.05,
        ask=mid + 0.05,
        delta=0.20 if option_type == "call" else -0.20,
        theta=theta,
        vega=vega,
        underlying_price=spot,
    )


def _csp_order(*, iv_rank: float = 40.0) -> StrategyOrder:
    short = _contract("put", 95.0, mid=1.20, theta=-0.05, vega=0.10)
    return StrategyOrder(
        strategy_name="cash_secured_put",
        strategy_id="csp-1",
        underlying="XYZ",
        legs=[OrderLeg(contract=short, side="sell_to_open", qty=1)],
        max_loss=9380.0,
        max_profit=120.0,
        required_buying_power=9380.0,
        profit_take_pct=0.5,
        loss_stop_multiple=2.0,
        roll_threshold_delta=-0.40,
        iv_rank=iv_rank,
    )


def _calendar_order(*, iv_rank: float = 25.0) -> StrategyOrder:
    front = _contract("call", 100.0, mid=2.0, theta=-0.08, vega=0.10)
    back = _contract("call", 100.0, mid=4.0, theta=-0.04, vega=0.20)
    # Net opening cashflow: sell front (+200) - buy back (-400) = -200 (debit)
    return StrategyOrder(
        strategy_name="calendar_spread",
        strategy_id="cal-1",
        underlying="XYZ",
        legs=[
            OrderLeg(contract=front, side="sell_to_open", qty=1),
            OrderLeg(contract=back, side="buy_to_open", qty=1),
        ],
        max_loss=200.0,
        max_profit=300.0,
        required_buying_power=200.0,
        profit_take_pct=0.30,
        loss_stop_multiple=1.0,
        roll_threshold_delta=None,
        iv_rank=iv_rank,
    )


class TradeIdeaCriticTests(unittest.TestCase):
    def test_clean_csp_passes(self):
        result = critique_trade(_csp_order())
        self.assertEqual(result["severity"], "none")

    def test_zero_legs_blocks(self):
        order = _csp_order()
        order.legs = []
        result = critique_trade(order)
        self.assertEqual(result["severity"], "major")
        self.assertIn("order has no legs", result["concerns"])

    def test_negative_max_loss_blocks(self):
        order = _csp_order()
        order.max_loss = -1.0
        result = critique_trade(order)
        self.assertEqual(result["severity"], "major")

    def test_short_premium_with_negative_theta_blocks(self):
        """A short-premium position with theta legs flipped should
        be vetoed; we are not collecting decay."""
        order = _csp_order()
        # Build a leg with positive theta (anomalous for an option),
        # which after the short sign-flip yields negative net theta.
        bad_contract = _contract("put", 95.0, mid=1.20, theta=0.05)
        order.legs = [OrderLeg(contract=bad_contract, side="sell_to_open", qty=1)]
        result = critique_trade(order)
        self.assertEqual(result["severity"], "major")
        self.assertTrue(any("negative net theta" in c for c in result["concerns"]))

    def test_short_call_below_spot_blocks(self):
        """Short call deep ITM is essentially never the right entry."""
        order = _csp_order()
        order.strategy_name = "covered_call"
        deep_itm = _contract("call", 80.0, spot=100.0, mid=20.0, theta=-0.02)
        order.legs = [OrderLeg(contract=deep_itm, side="sell_to_open", qty=1)]
        result = critique_trade(order)
        self.assertEqual(result["severity"], "major")
        self.assertTrue(any("deep ITM" in c for c in result["concerns"]))

    def test_short_put_above_spot_blocks(self):
        order = _csp_order()
        deep_itm = _contract("put", 110.0, spot=100.0, mid=12.0, theta=-0.02)
        order.legs = [OrderLeg(contract=deep_itm, side="sell_to_open", qty=1)]
        result = critique_trade(order)
        self.assertEqual(result["severity"], "major")

    def test_low_iv_rank_short_premium_minor(self):
        result = critique_trade(_csp_order(iv_rank=15.0))
        self.assertEqual(result["severity"], "minor")
        self.assertTrue(any("iv_rank" in c for c in result["concerns"]))

    def test_calendar_clean(self):
        result = critique_trade(_calendar_order())
        self.assertEqual(result["severity"], "none")

    def test_calendar_with_high_iv_rank_minor(self):
        result = critique_trade(_calendar_order(iv_rank=60.0))
        self.assertEqual(result["severity"], "minor")

    def test_short_premium_for_credit_sanity(self):
        """A 'short premium' order that opens for debit must be
        vetoed; the legs are wrong."""
        order = _csp_order()
        # Force a debit by inverting the leg side.
        order.legs[0] = OrderLeg(contract=order.legs[0].contract, side="buy_to_open", qty=1)
        result = critique_trade(order)
        self.assertEqual(result["severity"], "major")

    def test_critic_never_originates(self):
        """Sanity check: the critic returns analysis data only,
        never anything that could be interpreted as a trade
        instruction or upsize."""
        result = critique_trade(_csp_order())
        # Allowed keys only.
        self.assertEqual(
            set(result.keys()),
            {"concerns", "severity", "major", "minor", "net_theta", "net_vega", "net_credit"},
        )


if __name__ == "__main__":
    unittest.main()
