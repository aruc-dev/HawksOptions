"""Defined-risk collar strategy for existing long-stock inventory."""

from __future__ import annotations

from core.contract_selector import find_by_delta
from core.models import OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy


class CollarStrategy(BaseStrategy):
    name = "collar"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        if self.in_earnings_blackout(context):
            return None
        if not self.event_risk_filter_passes(context):
            return None
        share_contracts = int(context.long_shares // 100)
        if share_contracts <= 0:
            return None
        qty = min(share_contracts, self.order_quantity(context))
        if qty <= 0:
            return None
        put = find_by_delta(
            [
                contract
                for contract in self.filtered_chain(context, "put")
                if contract.strike < context.underlying_price
            ],
            float(self.params.get("put_delta", -0.20)),
            target_dte=int(self.params.get("target_dte", 35)),
            as_of=context.as_of,
        )
        call = find_by_delta(
            [
                contract
                for contract in self.filtered_chain(context, "call")
                if contract.strike > max(context.underlying_price, context.cost_basis)
            ],
            float(self.params.get("call_delta", 0.20)),
            target_dte=int(self.params.get("target_dte", 35)),
            as_of=context.as_of,
        )
        if put is None or call is None or put.expiration != call.expiration:
            return None
        net_credit = round((call.mid_price() - put.mid_price()) * 100.0 * qty, 2)
        stock_basis = context.cost_basis if context.cost_basis > 0 else context.underlying_price
        protected_stock_risk = max(0.0, stock_basis - put.strike) * 100.0 * qty
        capped_stock_profit = max(0.0, call.strike - stock_basis) * 100.0 * qty
        max_loss = round(protected_stock_risk - net_credit, 2)
        max_profit = round(capped_stock_profit + net_credit, 2)
        if max_loss <= 0 or max_profit <= 0:
            return None
        return StrategyOrder(
            strategy_name=self.name,
            strategy_id=self.strategy_id(context),
            underlying=context.underlying["symbol"],
            legs=[
                OrderLeg(contract=put, side="buy_to_open", qty=qty),
                OrderLeg(contract=call, side="sell_to_open", qty=qty),
            ],
            max_loss=max_loss,
            max_profit=max_profit,
            required_buying_power=max(0.0, -net_credit),
            profit_take_pct=float(self.params.get("profit_take_pct", 0.50)),
            loss_stop_multiple=float(self.params.get("loss_stop_multiple", 1.0)),
            roll_threshold_delta=float(self.params.get("roll_threshold_delta", 0.45)),
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 1)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=self.next_earnings_date(context),
            ex_dividend_date=self.ex_dividend_date(context),
            metadata={"stock_basis": stock_basis, "covered_shares": qty * 100},
        )
