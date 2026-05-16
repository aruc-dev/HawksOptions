"""Defined-risk diagonal spreads with front-leg assignment controls."""

from __future__ import annotations

from core.contract_selector import find_by_delta
from core.models import OptionContract, OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy


class DiagonalSpreadStrategy(BaseStrategy):
    name = "diagonal_spread"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        if self.in_earnings_blackout(context):
            return None
        if not self.event_risk_filter_passes(context):
            return None
        option_type = self._option_type()
        if option_type is None:
            return None
        contracts = self.filtered_chain(context, option_type)
        front_leg = find_by_delta(
            contracts,
            self._front_delta(option_type),
            target_dte=int(self.params.get("front_dte", 21)),
            as_of=context.as_of,
        )
        if front_leg is None:
            return None
        back_leg = self._select_covering_back_leg(contracts, front_leg, context)
        if back_leg is None:
            return None
        if self._short_call_ex_div_window(front_leg, context):
            return None
        debit = round((back_leg.mid_price() - front_leg.mid_price()) * 100.0, 2)
        if debit <= 0:
            return None
        max_profit = round(debit * float(self.params.get("profit_multiple_estimate", 1.5)), 2)
        if max_profit <= 0:
            return None
        qty = self.order_quantity(context)
        if qty <= 0:
            return None
        order = StrategyOrder(
            strategy_name=self.name,
            strategy_id=self.strategy_id(context),
            underlying=context.underlying["symbol"],
            legs=[
                OrderLeg(contract=front_leg, side="sell_to_open", qty=1),
                OrderLeg(contract=back_leg, side="buy_to_open", qty=1),
            ],
            max_loss=debit,
            max_profit=max_profit,
            required_buying_power=debit,
            profit_take_pct=float(self.params.get("profit_take_pct", 0.30)),
            loss_stop_multiple=float(self.params.get("loss_stop_multiple", 1.0)),
            roll_threshold_delta=None,
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 3)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=self.next_earnings_date(context),
            ex_dividend_date=self.ex_dividend_date(context),
            metadata={"front_expiration": front_leg.expiration.isoformat(), "back_expiration": back_leg.expiration.isoformat()},
        )
        return self.apply_contract_quantity(order, qty)

    def _option_type(self) -> str | None:
        variant = str(self.params.get("variant", "call_debit")).lower()
        if variant in {"call", "call_debit", "bullish"}:
            return "call"
        if variant in {"put", "put_debit", "bearish"}:
            return "put"
        return None

    def _front_delta(self, option_type: str) -> float:
        default = 0.30 if option_type == "call" else -0.30
        return float(self.params.get("front_delta", default))

    def _back_delta(self, option_type: str) -> float:
        default = 0.45 if option_type == "call" else -0.45
        return float(self.params.get("back_delta", default))

    def _select_covering_back_leg(
        self,
        contracts: list[OptionContract],
        front_leg: OptionContract,
        context: StrategyContext,
    ) -> OptionContract | None:
        back_dte = int(self.params.get("back_dte", 45))
        if front_leg.option_type == "call":
            candidates = [
                contract
                for contract in contracts
                if contract.expiration > front_leg.expiration
                and contract.strike <= front_leg.strike
                and contract.delta is not None
            ]
        else:
            candidates = [
                contract
                for contract in contracts
                if contract.expiration > front_leg.expiration
                and contract.strike >= front_leg.strike
                and contract.delta is not None
            ]
        if not candidates:
            return None
        target_delta = self._back_delta(front_leg.option_type)
        candidates.sort(
            key=lambda contract: (
                abs(contract.days_to_expiration(context.as_of) - back_dte),
                abs(abs(contract.delta or 0.0) - abs(target_delta)),
                abs(contract.strike - front_leg.strike),
                contract.spread_pct(),
            )
        )
        return candidates[0]

    def _short_call_ex_div_window(self, front_leg: OptionContract, context: StrategyContext) -> bool:
        ex_dividend_date = self.ex_dividend_date(context)
        if front_leg.option_type != "call" or ex_dividend_date is None:
            return False
        return context.as_of <= ex_dividend_date <= front_leg.expiration
