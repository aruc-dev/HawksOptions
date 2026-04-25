"""Deferred long-premium calendar spread."""

from __future__ import annotations

from collections import defaultdict

from core.contract_selector import find_by_delta
from core.models import OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy


class CalendarSpreadStrategy(BaseStrategy):
    name = "calendar_spread"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        calls = self.filtered_chain(context, "call")
        target = find_by_delta(
            calls,
            float(self.params.get("target_delta", 0.30)),
            target_dte=int(self.params.get("front_dte", 21)),
            as_of=context.as_of,
        )
        if target is None:
            return None
        by_strike = defaultdict(list)
        for contract in calls:
            if contract.strike == target.strike:
                by_strike[contract.strike].append(contract)
        same_strike = sorted(by_strike[target.strike], key=lambda contract: contract.expiration)
        if len(same_strike) < 2:
            return None
        front_leg = same_strike[0]
        back_leg = same_strike[-1]
        debit = round((back_leg.mid_price() - front_leg.mid_price()) * 100.0, 2)
        if debit <= 0:
            return None
        return StrategyOrder(
            strategy_name=self.name,
            strategy_id=self.strategy_id(context),
            underlying=context.underlying["symbol"],
            legs=[
                OrderLeg(contract=front_leg, side="sell_to_open", qty=1),
                OrderLeg(contract=back_leg, side="buy_to_open", qty=1),
            ],
            max_loss=debit,
            max_profit=round(debit * 1.5, 2),
            required_buying_power=debit,
            profit_take_pct=0.30,
            loss_stop_multiple=1.0,
            roll_threshold_delta=None,
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 3)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=self.next_earnings_date(context),
            ex_dividend_date=self.ex_dividend_date(context),
        )
