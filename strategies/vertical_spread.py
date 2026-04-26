"""Defined-risk vertical spreads."""

from __future__ import annotations

from core.contract_selector import select_vertical_spread
from core.models import OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy


class VerticalSpreadStrategy(BaseStrategy):
    name = "vertical_spread"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        if self.in_earnings_blackout(context):
            return None
        variant = str(self.params.get("variant", "bull_put_credit"))
        if variant == "auto":
            trend = float(context.underlying.get("trend_20d", 0.0))
            variant = "bull_put_credit" if trend >= 0 else "bear_call_credit"
        if variant in {"bull_put_credit", "bullish"}:
            option_type = "put"
        elif variant in {"bear_call_credit", "bearish"}:
            option_type = "call"
        else:
            return None
        pair = select_vertical_spread(
            self.filtered_chain(context, option_type),
            short_delta=float(self.params.get("short_delta", -0.25)),
            long_delta=float(self.params.get("long_delta", -0.10)),
            option_type=option_type,
            target_dte=int(self.params.get("target_dte", 35)),
            as_of=context.as_of,
        )
        if pair is None:
            return None
        short_leg, long_leg = pair
        width = abs(short_leg.strike - long_leg.strike) * 100.0
        credit = round((short_leg.mid_price() - long_leg.mid_price()) * 100.0, 2)
        if credit <= 0 or width <= credit:
            return None
        if not self.credit_quality_passes(credit=credit, width=width):
            return None
        qty = self.order_quantity(context)
        if qty <= 0:
            return None
        order = StrategyOrder(
            strategy_name=self.name,
            strategy_id=self.strategy_id(context),
            underlying=context.underlying["symbol"],
            legs=[
                OrderLeg(contract=short_leg, side="sell_to_open", qty=1),
                OrderLeg(contract=long_leg, side="buy_to_open", qty=1),
            ],
            max_loss=round(width - credit, 2),
            max_profit=credit,
            required_buying_power=round(width - credit, 2),
            profit_take_pct=float(self.params.get("profit_take_pct", 0.50)),
            loss_stop_multiple=float(self.params.get("loss_stop_multiple", 1.5)),
            roll_threshold_delta=float(self.params.get("roll_threshold_delta", -0.40)),
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 3)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=self.next_earnings_date(context),
            ex_dividend_date=self.ex_dividend_date(context),
        )
        return self.apply_contract_quantity(order, qty)
