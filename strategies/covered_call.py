"""Covered-call strategy."""

from __future__ import annotations

from core.contract_selector import find_by_delta
from core.models import OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy


class CoveredCallStrategy(BaseStrategy):
    name = "covered_call"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        if self.in_earnings_blackout(context):
            return None
        if context.long_shares < 100:
            return None
        eligible_calls = [
            contract
            for contract in self.filtered_chain(context, "call")
            if contract.strike > max(context.underlying_price, context.cost_basis)
        ]
        contract = find_by_delta(
            eligible_calls,
            float(self.params.get("target_delta", 0.25)),
            target_dte=int(self.params.get("target_dte", 35)),
            as_of=context.as_of,
        )
        if contract is None:
            return None
        qty = max(1, context.long_shares // 100)
        credit = round(contract.mid_price() * 100.0 * qty, 2)
        max_loss = round((max(context.cost_basis, context.underlying_price) * 100.0 * qty) - credit, 2)
        return StrategyOrder(
            strategy_name=self.name,
            strategy_id=self.strategy_id(context),
            underlying=context.underlying["symbol"],
            legs=[OrderLeg(contract=contract, side="sell_to_open", qty=qty)],
            max_loss=max_loss,
            max_profit=credit,
            required_buying_power=0.0,
            profit_take_pct=float(self.params.get("profit_take_pct", 0.50)),
            loss_stop_multiple=float(self.params.get("loss_stop_multiple", 2.0)),
            roll_threshold_delta=float(self.params.get("roll_threshold_delta", 0.45)),
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 1)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=self.next_earnings_date(context),
            ex_dividend_date=self.ex_dividend_date(context),
        )
