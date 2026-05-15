"""Defined-risk broken-wing butterfly strategy."""

from __future__ import annotations

from core.contract_selector import find_by_delta
from core.models import OptionContract, OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy


class BrokenWingButterflyStrategy(BaseStrategy):
    name = "broken_wing_butterfly"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        if self.in_earnings_blackout(context):
            return None
        if not self.event_risk_filter_passes(context):
            return None
        if not self.implied_realized_filter_passes(context):
            return None
        option_type = str(self.params.get("option_type", "put"))
        if not self.volatility_surface_filter_passes(context, option_type=option_type):
            return None
        contracts = self.filtered_chain(context, option_type)
        body = find_by_delta(
            contracts,
            float(self.params.get("body_delta", -0.25 if option_type == "put" else 0.25)),
            target_dte=int(self.params.get("target_dte", 35)),
            as_of=context.as_of,
        )
        if body is None:
            return None
        lower, upper = self._select_wings(contracts, body)
        if lower is None or upper is None:
            return None
        credit = round(((body.mid_price() * 2.0) - lower.mid_price() - upper.mid_price()) * 100.0, 2)
        lower_width = abs(body.strike - lower.strike) * 100.0
        upper_width = abs(upper.strike - body.strike) * 100.0
        max_width = max(lower_width, upper_width)
        min_width = min(lower_width, upper_width)
        max_loss = round(max_width - credit, 2)
        max_profit = round(min_width + credit, 2)
        if credit <= 0 or max_loss <= 0 or max_profit <= 0:
            return None
        if not self.credit_quality_passes(credit=credit, width=max_width):
            return None
        qty = self.order_quantity(context)
        if qty <= 0:
            return None
        order = StrategyOrder(
            strategy_name=self.name,
            strategy_id=self.strategy_id(context),
            underlying=context.underlying["symbol"],
            legs=[
                OrderLeg(contract=lower, side="buy_to_open", qty=1),
                OrderLeg(contract=body, side="sell_to_open", qty=2),
                OrderLeg(contract=upper, side="buy_to_open", qty=1),
            ],
            max_loss=max_loss,
            max_profit=credit if bool(self.params.get("credit_only_max_profit", True)) else max_profit,
            required_buying_power=max_loss,
            profit_take_pct=float(self.params.get("profit_take_pct", 0.35)),
            loss_stop_multiple=float(self.params.get("loss_stop_multiple", 1.5)),
            roll_threshold_delta=float(self.params.get("roll_threshold_delta", 0.45)),
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 3)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=self.next_earnings_date(context),
            ex_dividend_date=self.ex_dividend_date(context),
        )
        return self.apply_contract_quantity(order, qty)

    def _select_wings(
        self,
        contracts: list[OptionContract],
        body: OptionContract,
    ) -> tuple[OptionContract | None, OptionContract | None]:
        same_expiration = [
            contract
            for contract in contracts
            if contract.expiration == body.expiration and contract.strike != body.strike
        ]
        lower_candidates = [contract for contract in same_expiration if contract.strike < body.strike]
        upper_candidates = [contract for contract in same_expiration if contract.strike > body.strike]
        min_width = float(self.params.get("min_wing_width", 1.0))
        ratio = max(1.0, float(self.params.get("broken_wing_ratio", 2.0)))
        lower_candidates = [
            contract
            for contract in lower_candidates
            if abs(body.strike - contract.strike) >= min_width
        ]
        if not lower_candidates:
            return None, None
        lower_candidates.sort(key=lambda contract: (abs(body.strike - contract.strike), contract.spread_pct()))
        lower = lower_candidates[0]
        lower_width = abs(body.strike - lower.strike)
        target_upper_width = lower_width * ratio
        upper_candidates = [
            contract
            for contract in upper_candidates
            if abs(contract.strike - body.strike) >= min_width
        ]
        if not upper_candidates:
            return None, None
        upper_candidates.sort(
            key=lambda contract: (
                abs(abs(contract.strike - body.strike) - target_upper_width),
                contract.spread_pct(),
            )
        )
        return lower, upper_candidates[0]
