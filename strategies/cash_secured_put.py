"""Cash-secured put strategy."""

from __future__ import annotations

from core.contract_selector import find_by_delta
from core.models import OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy


class CashSecuredPutStrategy(BaseStrategy):
    name = "cash_secured_put"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        if self.in_earnings_blackout(context):
            return None
        if not self.event_risk_filter_passes(context):
            return None
        if not self.technical_regime_filter_passes(context):
            return None
        puts = self.filtered_chain(context, "put")
        contract = find_by_delta(
            puts,
            float(self.params.get("target_delta", -0.20)),
            target_dte=int(self.params.get("target_dte", 35)),
            as_of=context.as_of,
        )
        if contract is None:
            return None
        credit = round(contract.mid_price() * 100.0, 2)
        max_loss = round((contract.strike * 100.0) - credit, 2)
        qty = self.order_quantity(context)
        if qty <= 0:
            return None
        order = StrategyOrder(
            strategy_name=self.name,
            strategy_id=self.strategy_id(context),
            underlying=context.underlying["symbol"],
            legs=[OrderLeg(contract=contract, side="sell_to_open", qty=1)],
            max_loss=max_loss,
            max_profit=credit,
            required_buying_power=round(contract.strike * 100.0, 2),
            profit_take_pct=float(self.params.get("profit_take_pct", 0.50)),
            loss_stop_multiple=float(self.params.get("loss_stop_multiple", 2.0)),
            roll_threshold_delta=float(self.params.get("roll_threshold_delta", -0.40)),
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 1)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=self.next_earnings_date(context),
            ex_dividend_date=self.ex_dividend_date(context),
        )
        return self.apply_contract_quantity(order, qty)
