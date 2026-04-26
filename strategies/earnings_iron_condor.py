"""Optional earnings iron condor."""

from __future__ import annotations

from datetime import timedelta

from core.contract_selector import select_iron_condor
from core.models import OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy


class EarningsIronCondorStrategy(BaseStrategy):
    name = "earnings_iron_condor"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        earnings_date = self.next_earnings_date(context)
        entry_days_before = int(self.params.get("entry_days_before_earnings", 1))
        if earnings_date is None or earnings_date != (context.as_of + timedelta(days=entry_days_before)):
            return None
        if context.iv_rank < float(self.params.get("require_iv_rank", 60)):
            return None
        contracts = select_iron_condor(
            context.chain,
            put_short_delta=float(self.params.get("put_short_delta", -0.16)),
            put_long_delta=float(self.params.get("put_long_delta", -0.08)),
            call_short_delta=float(self.params.get("call_short_delta", 0.16)),
            call_long_delta=float(self.params.get("call_long_delta", 0.08)),
            target_dte=int(self.params.get("dte_min", 7)),
            as_of=context.as_of,
        )
        if contracts is None:
            return None
        short_put, long_put, short_call, long_call = contracts
        credit = round(
            (short_put.mid_price() + short_call.mid_price() - long_put.mid_price() - long_call.mid_price()) * 100.0,
            2,
        )
        max_loss = round(max(abs(short_put.strike - long_put.strike), abs(long_call.strike - short_call.strike)) * 100.0 - credit, 2)
        if credit <= 0 or max_loss <= 0:
            return None
        if not self.credit_quality_passes(credit=credit, width=max_loss + credit):
            return None
        qty = self.order_quantity(context)
        if qty <= 0:
            return None
        order = StrategyOrder(
            strategy_name=self.name,
            strategy_id=self.strategy_id(context),
            underlying=context.underlying["symbol"],
            legs=[
                OrderLeg(contract=short_put, side="sell_to_open", qty=1),
                OrderLeg(contract=long_put, side="buy_to_open", qty=1),
                OrderLeg(contract=short_call, side="sell_to_open", qty=1),
                OrderLeg(contract=long_call, side="buy_to_open", qty=1),
            ],
            max_loss=max_loss,
            max_profit=credit,
            required_buying_power=max_loss,
            profit_take_pct=float(self.params.get("profit_take_pct", 0.25)),
            loss_stop_multiple=float(self.params.get("loss_stop_multiple", 1.25)),
            roll_threshold_delta=float(self.params.get("warning_delta_abs", 0.35)),
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 3)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=earnings_date,
            ex_dividend_date=self.ex_dividend_date(context),
        )
        return self.apply_contract_quantity(order, qty)
