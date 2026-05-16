"""Defined-risk symmetric butterfly strategy templates."""

from __future__ import annotations

from core.contract_selector import find_by_delta
from core.models import OptionContract, OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy


class ButterflyStrategy(BaseStrategy):
    name = "butterfly"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        if self.in_earnings_blackout(context):
            return None
        if not self.event_risk_filter_passes(context):
            return None
        option_type = str(self.params.get("option_type", "put"))
        variant = str(self.params.get("variant", "long_debit"))
        if variant in {"short", "short_credit", "credit"} and not self.implied_realized_filter_passes(context):
            return None
        if variant in {"short", "short_credit", "credit"} and not self.volatility_surface_filter_passes(context, option_type=option_type):
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
        lower, upper = self._select_symmetric_wings(contracts, body)
        if lower is None or upper is None:
            return None
        width = round(abs(body.strike - lower.strike) * 100.0, 2)
        if width <= 0:
            return None
        if variant in {"long", "long_debit", "debit"}:
            return self._long_butterfly(context, lower, body, upper, width)
        if variant in {"short", "short_credit", "credit"}:
            return self._short_butterfly(context, lower, body, upper, width)
        return None

    def _select_symmetric_wings(
        self,
        contracts: list[OptionContract],
        body: OptionContract,
    ) -> tuple[OptionContract | None, OptionContract | None]:
        min_width = float(self.params.get("min_wing_width", 1.0))
        max_width_mismatch = float(self.params.get("max_wing_width_mismatch", 0.0))
        same_expiration = [
            contract
            for contract in contracts
            if contract.expiration == body.expiration and contract.strike != body.strike
        ]
        lower_candidates = [
            contract
            for contract in same_expiration
            if contract.strike < body.strike and abs(body.strike - contract.strike) >= min_width
        ]
        upper_candidates = [
            contract
            for contract in same_expiration
            if contract.strike > body.strike and abs(contract.strike - body.strike) >= min_width
        ]
        pairs = []
        for lower in lower_candidates:
            lower_width = abs(body.strike - lower.strike)
            for upper in upper_candidates:
                upper_width = abs(upper.strike - body.strike)
                width_mismatch = abs(lower_width - upper_width)
                if width_mismatch > max_width_mismatch:
                    continue
                pairs.append((width_mismatch, lower.spread_pct() + upper.spread_pct(), lower, upper))
        if not pairs:
            return None, None
        pairs.sort(key=lambda item: (item[0], item[1]))
        _, _, lower, upper = pairs[0]
        return lower, upper

    def _long_butterfly(
        self,
        context: StrategyContext,
        lower: OptionContract,
        body: OptionContract,
        upper: OptionContract,
        width: float,
    ) -> StrategyOrder | None:
        debit = round((lower.mid_price() + upper.mid_price() - (2.0 * body.mid_price())) * 100.0, 2)
        if debit <= 0 or debit >= width:
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
            max_loss=debit,
            max_profit=round(width - debit, 2),
            required_buying_power=debit,
            profit_take_pct=float(self.params.get("profit_take_pct", 0.50)),
            loss_stop_multiple=float(self.params.get("loss_stop_multiple", 1.0)),
            roll_threshold_delta=float(self.params.get("roll_threshold_delta", 0.45)),
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 3)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=self.next_earnings_date(context),
            ex_dividend_date=self.ex_dividend_date(context),
        )
        return self.apply_contract_quantity(order, qty)

    def _short_butterfly(
        self,
        context: StrategyContext,
        lower: OptionContract,
        body: OptionContract,
        upper: OptionContract,
        width: float,
    ) -> StrategyOrder | None:
        credit = round((lower.mid_price() + upper.mid_price() - (2.0 * body.mid_price())) * 100.0, 2)
        if credit <= 0 or credit >= width:
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
                OrderLeg(contract=lower, side="sell_to_open", qty=1),
                OrderLeg(contract=body, side="buy_to_open", qty=2),
                OrderLeg(contract=upper, side="sell_to_open", qty=1),
            ],
            max_loss=round(width - credit, 2),
            max_profit=credit,
            required_buying_power=round(width - credit, 2),
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
