"""Base interface and shared helpers for options strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from core.config import strategy_config
from core.event_risk import event_risk_passes
from core.models import OrderLeg, StrategyContext, StrategyOrder
from core.options_chain import filter_contracts
from core.technical_regime import technical_regime_context, technical_regime_passes
from core.volatility_surface import volatility_surface_metrics


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
        earnings_date = _as_date(context.next_earnings_date or context.underlying.get("next_earnings_date"))
        if earnings_date is None or earnings_date < context.as_of:
            return None
        return earnings_date

    def ex_dividend_date(self, context: StrategyContext) -> date | None:
        return _as_date(context.ex_dividend_date or context.underlying.get("ex_dividend_date"))

    def in_earnings_blackout(self, context: StrategyContext) -> bool:
        earnings_date = self.next_earnings_date(context)
        if earnings_date is None:
            return False
        blackout_days = int(self.config.get("gates", {}).get("earnings_blackout_days_before", 5))
        days_to_earnings = (earnings_date - context.as_of).days
        return 0 <= days_to_earnings <= blackout_days

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

    def modeled_entry_cost(self, legs: list[OrderLeg]) -> float:
        """Estimate configured entry friction without importing backtest code."""
        slippage = self.config.get("backtest", {}).get("slippage", {})
        if not isinstance(slippage, dict):
            slippage = {}
        per_leg_cents = float(slippage.get("per_leg_cents", 0.0))
        spread_pct = float(slippage.get("spread_pct", 0.0))
        commission = float(slippage.get("commission_per_contract", 0.0))
        cost = 0.0
        for leg in legs:
            qty = max(1, int(leg.qty))
            spread = max(0.0, float(leg.contract.ask) - float(leg.contract.bid))
            cost += ((per_leg_cents + (spread_pct * spread / 2.0)) * 100.0 * qty) + (commission * qty)
        return round(cost, 4)

    def cost_adjusted_credit_passes(self, *, credit: float, legs: list[OrderLeg]) -> bool:
        min_credit_to_roundtrip_cost = float(self.params.get("min_credit_to_roundtrip_cost", 0.0))
        if min_credit_to_roundtrip_cost <= 0:
            return True
        roundtrip_cost = self.modeled_entry_cost(legs) * 2.0
        if roundtrip_cost <= 0:
            return True
        return credit >= roundtrip_cost * min_credit_to_roundtrip_cost

    def implied_realized_spread(self, context: StrategyContext) -> float:
        return round(float(context.current_iv or 0.0) - float(context.realized_vol_20d or 0.0), 6)

    def implied_realized_filter_passes(self, context: StrategyContext) -> bool:
        current_iv = float(context.current_iv or context.underlying.get("current_iv", context.iv_rank / 100.0))
        realized_vol = float(context.realized_vol_20d or 0.0)
        if current_iv <= 0 or realized_vol <= 0:
            return True
        min_spread = float(self.params.get("min_iv_realized_spread", 0.0))
        min_ratio = float(self.params.get("min_iv_over_realized_vol", 0.0))
        if min_spread > 0 and (current_iv - realized_vol) < min_spread:
            return False
        if min_ratio > 0 and current_iv < realized_vol * min_ratio:
            return False
        return True

    def surface_metrics(self, context: StrategyContext) -> dict[str, float | None]:
        return volatility_surface_metrics(
            context.chain,
            underlying_price=context.underlying_price,
            as_of=context.as_of,
            front_dte=int(self.params.get("surface_front_dte", self.params.get("target_dte", 30))),
            back_dte=int(self.params.get("surface_back_dte", 45)),
        )

    def volatility_surface_filter_passes(self, context: StrategyContext, *, option_type: str | None = None) -> bool:
        metrics = self.surface_metrics(context)
        if option_type in {"put", "call"}:
            skew = metrics.get(f"{option_type}_tail_skew")
            min_skew = float(self.params.get(f"min_{option_type}_tail_skew", self.params.get("min_tail_skew", 0.0)))
            if min_skew > 0 and skew is not None and skew < min_skew:
                return False
        slope = metrics.get("term_structure_slope")
        min_slope = self.params.get("min_term_structure_slope")
        max_slope = self.params.get("max_term_structure_slope")
        if min_slope is not None and slope is not None and slope < float(min_slope):
            return False
        if max_slope is not None and slope is not None and slope > float(max_slope):
            return False
        return True

    def technical_regime_metrics(self, context: StrategyContext) -> dict[str, float | None]:
        metrics = technical_regime_context(context.underlying)
        overrides = {
            "trend_20d": context.trend_20d,
            "trend_50d": context.trend_50d,
            "rsi_14": context.rsi_14,
            "price_vs_sma_50": context.price_vs_sma_50,
        }
        for key, value in overrides.items():
            if value is not None:
                metrics[key] = float(value)
        return metrics

    def technical_regime_filter_passes(self, context: StrategyContext) -> bool:
        return technical_regime_passes(self.technical_regime_metrics(context), self.params)

    def event_risk_filter_passes(self, context: StrategyContext) -> bool:
        return event_risk_passes(context.underlying, self.params)

    def strategy_id(self, context: StrategyContext) -> str:
        return f"{self.name}-{context.underlying['symbol']}-{context.as_of:%Y%m%d}"

    @abstractmethod
    def generate_order(self, context: StrategyContext):  # pragma: no cover - interface only
        raise NotImplementedError
