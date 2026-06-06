"""Strategy candidate scoring and selection helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.config import strategy_config
from core.dealer_positioning import dealer_positioning_context
from core.event_risk import event_risk_context
from core.models import StrategyContext, StrategyOrder
from core.open_interest_analytics import open_interest_context
from core.strategy_types import LONG_PREMIUM_STRATEGIES
from core.technical_regime import technical_regime_context
from core.volatility_surface import volatility_surface_metrics

_DEFAULT_SCORING_WEIGHTS = {
    "reward_to_risk": 1.0,
    "iv_component": 0.25,
    "regime_edge": 0.25,
    "max_pain_alignment": 0.05,
    "credit_to_width": 0.35,
    "theta_efficiency": 0.20,
    "liquidity": 0.20,
    "dte_fit": 0.15,
    "event_proximity": 0.15,
    "gamma_safety": 0.15,
    "vega_safety": 0.10,
    "portfolio_greek_room": 0.10,
}
_CONTEXT_ANALYTICS_CACHE_MAX_SIZE = 512
_CONTEXT_ANALYTICS_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def strategy_weight(config: dict[str, Any], strategy_name: str) -> float:
    return float(strategy_config(config, strategy_name).get("weight", 1.0))


@dataclass(frozen=True)
class StrategyCandidate:
    order: StrategyOrder
    context: StrategyContext
    score: float
    structural_severity: str = "none"
    warnings: list[str] = field(default_factory=list)

    @property
    def underlying(self) -> str:
        return self.order.underlying

    @property
    def strategy_name(self) -> str:
        return self.order.strategy_name

    def summary(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "strategy": self.strategy_name,
            "score": round(float(self.score), 6),
            "warnings": self.warnings,
            "net_opening_credit": self.order.net_opening_credit,
            "max_profit": self.order.max_profit,
            "max_loss": self.order.max_loss,
            "required_buying_power": self.order.required_buying_power,
            "iv_rank": self.order.iv_rank,
            "selection": dict(self.order.metadata.get("selection", {})),
        }


def score_order(order: StrategyOrder, context: StrategyContext, config: dict[str, Any]) -> float:
    """Return a deterministic score for ranking accepted candidates.

    The score is advisory only. It ranks orders that strategy constructors and
    pre-trade risk gates have already accepted; it never overrides hard safety
    checks.
    """
    weight = strategy_weight(config, order.strategy_name)
    params = strategy_config(config, order.strategy_name)
    scoring_weights = _scoring_weights(config)
    risk = max(float(order.max_loss), 0.01)
    reward_to_risk = max(float(order.max_profit), 0.0) / risk
    reward_to_risk_component = min(reward_to_risk, 5.0)
    iv_component = max(0.0, min(1.0, float(order.iv_rank) / 100.0))
    if order.strategy_name in LONG_PREMIUM_STRATEGIES:
        iv_component = 1.0 - iv_component
    current_iv = max(float(context.current_iv or 0.0), 0.01)
    implied_realized_spread = round(current_iv - float(context.realized_vol_20d or 0.0), 6)
    regime_edge = max(0.0, implied_realized_spread / current_iv)
    analytics = _context_analytics(context)
    surface = analytics["surface"]
    oi_context = analytics["open_interest"]
    dealer_context = analytics["dealer_positioning"]
    technical_context = dict(analytics["technical_regime"])
    event_context = analytics["event_risk"]
    max_pain_distance = oi_context.get("max_pain_distance_pct")
    max_pain_alignment = 0.0
    if max_pain_distance is not None:
        max_pain_alignment = max(0.0, 1.0 - abs(float(max_pain_distance)))
    greeks = _order_greeks(order)
    credit_to_width = _credit_to_width(order)
    theta_efficiency = _theta_efficiency(greeks["theta"], risk)
    liquidity = _liquidity_score(order)
    dte_fit = _dte_fit_score(order, context, params)
    event_proximity = _event_proximity_score(context)
    gamma_safety = _greek_safety_score(
        greeks["gamma_abs"] * (context.underlying_price ** 2) * 0.01,
        risk,
    )
    vega_safety = _greek_safety_score(greeks["vega_abs"] * max(current_iv, 0.01), risk)
    portfolio_greek_room = _portfolio_greek_room_score(context, greeks)
    score_components = {
        "reward_to_risk": reward_to_risk_component,
        "iv_component": iv_component,
        "regime_edge": regime_edge,
        "max_pain_alignment": max_pain_alignment,
        "credit_to_width": credit_to_width,
        "theta_efficiency": theta_efficiency,
        "liquidity": liquidity,
        "dte_fit": dte_fit,
        "event_proximity": event_proximity,
        "gamma_safety": gamma_safety,
        "vega_safety": vega_safety,
        "portfolio_greek_room": portfolio_greek_room,
    }
    component_total = sum(scoring_weights[key] * score_components[key] for key in _DEFAULT_SCORING_WEIGHTS)
    score = weight * (1.0 + component_total)
    order.metadata["selection_score"] = round(score, 6)
    order.metadata["selection"] = {
        "strategy_weight": round(weight, 6),
        "reward_to_risk": round(reward_to_risk, 6),
        "credit_to_width": round(credit_to_width, 6),
        "theta_dollars": round(greeks["theta"], 6),
        "gamma_abs": round(greeks["gamma_abs"], 6),
        "vega_abs": round(greeks["vega_abs"], 6),
        "liquidity_score": round(liquidity, 6),
        "dte_fit_score": round(dte_fit, 6),
        "event_proximity_score": round(event_proximity, 6),
        "gamma_safety_score": round(gamma_safety, 6),
        "vega_safety_score": round(vega_safety, 6),
        "portfolio_greek_room_score": round(portfolio_greek_room, 6),
        "score_components": {key: round(value, 6) for key, value in score_components.items()},
        "score_weights": {key: round(float(scoring_weights[key]), 6) for key in _DEFAULT_SCORING_WEIGHTS},
        "iv_component": round(iv_component, 6),
        "implied_realized_spread": implied_realized_spread,
        "regime_edge": round(regime_edge, 6),
        "put_tail_skew": surface["put_tail_skew"],
        "call_tail_skew": surface["call_tail_skew"],
        "term_structure_slope": surface["term_structure_slope"],
        "max_pain_strike": oi_context["max_pain_strike"],
        "max_pain_distance_pct": oi_context["max_pain_distance_pct"],
        "largest_oi_strike": oi_context["largest_oi_strike"],
        "largest_oi": oi_context["largest_oi"],
        "total_open_interest": oi_context["total_open_interest"],
        "dealer_positioning": dealer_context,
        "technical_regime": technical_context,
        "event_risk": event_context,
    }
    return score


def _scoring_weights(config: dict[str, Any]) -> dict[str, float]:
    weights = dict(_DEFAULT_SCORING_WEIGHTS)
    section = config.get("selection_scoring", {})
    if not isinstance(section, dict):
        return weights
    overrides = section.get("weights", {})
    if not isinstance(overrides, dict):
        return weights
    for key, value in overrides.items():
        if key not in weights:
            continue
        try:
            weights[key] = float(value)
        except (TypeError, ValueError):
            continue
    return weights


def _context_analytics(context: StrategyContext) -> dict[str, Any]:
    key = _context_analytics_cache_key(context)
    cached = _CONTEXT_ANALYTICS_CACHE.get(key)
    if cached is not None:
        return cached
    technical_context = technical_regime_context(context.underlying)
    for field_name in ("trend_20d", "trend_50d", "rsi_14", "price_vs_sma_50"):
        value = getattr(context, field_name)
        if value is not None:
            technical_context[field_name] = value
    analytics = {
        "surface": volatility_surface_metrics(
            context.chain,
            underlying_price=context.underlying_price,
            as_of=context.as_of,
        ),
        "open_interest": open_interest_context(context.chain, underlying_price=context.underlying_price),
        "dealer_positioning": dealer_positioning_context(
            context.underlying,
            as_of=context.as_of,
            underlying_price=context.underlying_price,
        ),
        "technical_regime": technical_context,
        "event_risk": event_risk_context(context.underlying),
    }
    if len(_CONTEXT_ANALYTICS_CACHE) >= _CONTEXT_ANALYTICS_CACHE_MAX_SIZE:
        _CONTEXT_ANALYTICS_CACHE.clear()
    _CONTEXT_ANALYTICS_CACHE[key] = analytics
    return analytics


def _context_analytics_cache_key(context: StrategyContext) -> tuple[Any, ...]:
    return (
        id(context),
        id(context.chain),
        len(context.chain),
        context.underlying.get("symbol"),
        context.underlying_price,
        context.as_of,
        context.underlying.get("event_risk"),
        context.underlying.get("event_risk_level"),
        context.underlying.get("event_risk_reason"),
        tuple(contract.contract_symbol for contract in context.chain[:20]),
    )


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, value))


def _credit_to_width(order: StrategyOrder) -> float:
    credit = max(float(order.net_opening_credit), 0.0)
    width = credit + max(float(order.max_loss), 0.0)
    if width <= 0:
        return 0.0
    return _clamp(credit / width)


def _order_greeks(order: StrategyOrder) -> dict[str, float]:
    totals = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0, "gamma_abs": 0.0, "vega_abs": 0.0}
    for leg in order.legs:
        sign = 1.0 if leg.side == "buy_to_open" else -1.0
        multiplier = 100.0 * leg.qty
        delta = float(leg.contract.delta or 0.0) * sign * multiplier
        theta = float(leg.contract.theta or 0.0) * sign * multiplier
        vega = float(leg.contract.vega or 0.0) * sign * multiplier
        gamma = float(leg.contract.gamma or 0.0) * sign * multiplier
        totals["delta"] += delta
        totals["theta"] += theta
        totals["vega"] += vega
        totals["gamma"] += gamma
        totals["gamma_abs"] += abs(gamma)
        totals["vega_abs"] += abs(vega)
    return totals


def _theta_efficiency(theta_dollars: float, risk: float) -> float:
    return _clamp(max(theta_dollars, 0.0) / max(risk, 0.01))


def _liquidity_score(order: StrategyOrder) -> float:
    if not order.legs:
        return 0.0
    open_interest = sum(max(int(leg.contract.open_interest), 0) for leg in order.legs) / len(order.legs)
    volume = sum(max(int(leg.contract.volume), 0) for leg in order.legs) / len(order.legs)
    spreads = [leg.contract.spread_pct() for leg in order.legs]
    finite_spreads = [spread for spread in spreads if spread != float("inf")]
    avg_spread = sum(finite_spreads) / len(finite_spreads) if finite_spreads else 1.0
    open_interest_score = _clamp(open_interest / 1000.0)
    volume_score = _clamp(volume / 100.0)
    spread_score = _clamp(1.0 - (avg_spread / 0.20))
    return (open_interest_score + volume_score + spread_score) / 3.0


def _dte_fit_score(order: StrategyOrder, context: StrategyContext, params: dict[str, Any]) -> float:
    if not order.legs:
        return 0.0
    dtes = [leg.contract.days_to_expiration(context.as_of) for leg in order.legs]
    avg_dte = sum(dtes) / len(dtes)
    target_dte = params.get("target_dte")
    if target_dte is None:
        min_dte = params.get("target_dte_min", params.get("dte_min"))
        max_dte = params.get("target_dte_max", params.get("dte_max"))
        if min_dte is not None and max_dte is not None:
            target_dte = (float(min_dte) + float(max_dte)) / 2.0
    if target_dte is None or float(target_dte) <= 0:
        return 0.5
    return _clamp(1.0 - (abs(avg_dte - float(target_dte)) / max(float(target_dte), 1.0)))


def _event_proximity_score(context: StrategyContext) -> float:
    event_dates = [context.next_earnings_date, context.ex_dividend_date]
    future_days = [(event_date - context.as_of).days for event_date in event_dates if event_date is not None]
    future_days = [days for days in future_days if days >= 0]
    if not future_days:
        return 1.0
    return _clamp(min(future_days) / 21.0)


def _greek_safety_score(exposure: float, risk: float) -> float:
    return _clamp(1.0 - (abs(exposure) / max(risk, 0.01)))


def _portfolio_greek_room_score(context: StrategyContext, order_greeks: dict[str, float]) -> float:
    limits = context.config.get("portfolio_greek_limits") or context.account.get("greek_limits")
    if not isinstance(limits, dict) or not limits:
        return 1.0
    current = context.account.get("portfolio_greeks", {})
    scores = []
    for greek in ("delta", "theta", "vega", "gamma"):
        limit = float(limits.get(greek, 0.0) or 0.0)
        if limit <= 0:
            continue
        projected = abs(float(current.get(greek, 0.0) or 0.0) + float(order_greeks.get(greek, 0.0)))
        scores.append(_clamp(1.0 - (projected / limit)))
    return min(scores) if scores else 1.0


def build_candidate(
    order: StrategyOrder,
    *,
    context: StrategyContext,
    config: dict[str, Any],
    structural_severity: str = "none",
    warnings: list[str] | None = None,
) -> StrategyCandidate:
    return StrategyCandidate(
        order=order,
        context=context,
        score=score_order(order, context, config),
        structural_severity=structural_severity,
        warnings=list(warnings or []),
    )


def rank_candidates(candidates: list[StrategyCandidate]) -> list[StrategyCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.score,
            -candidate.order.required_buying_power,
            candidate.order.net_opening_credit,
            candidate.underlying,
            candidate.strategy_name,
        ),
        reverse=True,
    )


def select_best_order(
    candidates: list[tuple[float, StrategyOrder]],
) -> StrategyOrder | None:
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]
