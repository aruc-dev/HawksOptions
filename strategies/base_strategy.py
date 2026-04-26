"""Base interface and shared helpers for options strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from core.config import strategy_config
from core.models import OrderLeg, StrategyContext, StrategyOrder
from core.options_chain import filter_contracts


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


class BaseStrategy(ABC):
    name = "base"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.params = strategy_config(config, self.name)

    def enabled(self) -> bool:
        return bool(self.params.get("enabled", False))

    def allowed_for_underlying(self, context: StrategyContext) -> bool:
        return self.name in list(context.underlying.get("strategies_allowed", []))

    def next_earnings_date(self, context: StrategyContext) -> date | None:
        return _as_date(context.next_earnings_date or context.underlying.get("next_earnings_date"))

    def ex_dividend_date(self, context: StrategyContext) -> date | None:
        return _as_date(context.ex_dividend_date or context.underlying.get("ex_dividend_date"))

    def in_earnings_blackout(self, context: StrategyContext) -> bool:
        earnings_date = self.next_earnings_date(context)
        if earnings_date is None:
            return False
        blackout_days = int(self.config.get("gates", {}).get("earnings_blackout_days_before", 5))
        return (earnings_date - context.as_of).days <= blackout_days

    def filtered_chain(self, context: StrategyContext, option_type: str) -> list:
        gates = self.config.get("gates", {})
        min_dte = int(self.params.get("target_dte_min", self.params.get("dte_min", gates.get("min_dte_entry", 7))))
        max_dte = int(self.params.get("target_dte_max", self.params.get("dte_max", gates.get("max_dte_entry", 55))))
        return filter_contracts(
            context.chain,
            min_open_interest=int(gates.get("min_open_interest", 100)),
            min_daily_volume=int(gates.get("min_daily_volume", 10)),
            max_bid_ask_spread_pct=float(gates.get("max_bid_ask_spread_pct", 0.10)),
            min_dte=min_dte,
            max_dte=max_dte,
            as_of=context.as_of,
            option_type=option_type,
        )

    def order_quantity(self, context: StrategyContext, *, default: int = 1) -> int:
        requested = int(self.params.get("contracts", self.params.get("qty", default)))
        limits = [requested]
        for key in ("max_contracts", "max_contracts_per_underlying"):
            if key in self.params:
                limits.append(int(self.params[key]))
            if key in context.underlying:
                limits.append(int(context.underlying[key]))
        non_negative_limits = [limit for limit in limits if limit >= 0]
        if not non_negative_limits:
            return 0
        return min(non_negative_limits)

    def apply_contract_quantity(self, order: StrategyOrder, qty: int) -> StrategyOrder:
        qty = max(1, int(qty))
        if qty == 1:
            return order
        order.legs = [
            OrderLeg(contract=leg.contract, side=leg.side, qty=leg.qty * qty)
            for leg in order.legs
        ]
        order.max_loss = round(order.max_loss * qty, 2)
        order.max_profit = round(order.max_profit * qty, 2)
        order.required_buying_power = round(order.required_buying_power * qty, 2)
        return order

    def credit_quality_passes(self, *, credit: float, width: float) -> bool:
        if width <= 0:
            return False
        min_credit = float(self.params.get("min_net_credit", 0.0))
        min_credit_to_width = float(self.params.get("min_credit_to_width", 0.0))
        max_loss = max(0.0, width - credit)
        min_reward_to_risk = float(self.params.get("min_reward_to_risk", 0.0))
        if credit < min_credit:
            return False
        if credit / width < min_credit_to_width:
            return False
        if min_reward_to_risk > 0 and (max_loss <= 0 or credit / max_loss < min_reward_to_risk):
            return False
        return True

    def strategy_id(self, context: StrategyContext) -> str:
        return f"{self.name}-{context.underlying['symbol']}-{context.as_of:%Y%m%d}"

    @abstractmethod
    def generate_order(self, context: StrategyContext):  # pragma: no cover - interface only
        raise NotImplementedError
