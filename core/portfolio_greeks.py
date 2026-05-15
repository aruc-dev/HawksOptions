"""Portfolio Greek aggregation and optional ceiling checks."""

from __future__ import annotations

from typing import Any, Iterable

from core.models import PositionSnapshot, StrategyOrder


def order_greeks(order: StrategyOrder) -> dict[str, float]:
    totals = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
    for leg in order.legs:
        sign = -1.0 if leg.side == "sell_to_open" else 1.0
        qty_scale = 100.0 * leg.qty
        totals["delta"] += sign * qty_scale * float(leg.contract.delta or 0.0)
        totals["theta"] += sign * qty_scale * float(leg.contract.theta or 0.0)
        totals["vega"] += sign * qty_scale * float(leg.contract.vega or 0.0)
        totals["gamma"] += sign * qty_scale * float(leg.contract.gamma or 0.0)
    return totals


def position_greeks(position: PositionSnapshot) -> dict[str, float]:
    totals = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
    for leg in position.legs:
        sign = -1.0 if leg.side == "sell_to_open" else 1.0
        qty_scale = 100.0 * leg.qty
        totals["delta"] += sign * qty_scale * float(leg.contract.delta or 0.0)
        totals["theta"] += sign * qty_scale * float(leg.contract.theta or 0.0)
        totals["vega"] += sign * qty_scale * float(leg.contract.vega or 0.0)
        totals["gamma"] += sign * qty_scale * float(leg.contract.gamma or 0.0)
    return totals


def aggregate_position_greeks(positions: Iterable[PositionSnapshot]) -> dict[str, float]:
    totals = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
    for position in positions:
        greeks = position_greeks(position)
        for key, value in greeks.items():
            totals[key] += value
    return totals


def greek_limit_reasons(
    order: StrategyOrder,
    *,
    open_positions: Iterable[PositionSnapshot],
    account: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    limits = _greek_limits(account, config)
    if not limits:
        return []
    projected = aggregate_position_greeks(open_positions)
    incoming = order_greeks(order)
    reasons = []
    for greek, limit in limits.items():
        if abs(projected.get(greek, 0.0) + incoming.get(greek, 0.0)) > limit:
            reasons.append(f"portfolio_{greek}_limit_exceeded")
    return reasons


def _greek_limits(account: dict[str, Any], config: dict[str, Any]) -> dict[str, float]:
    raw = config.get("portfolio_greek_limits") or account.get("greek_limits") or {}
    if not isinstance(raw, dict):
        return {}
    limits = {}
    for greek in ("delta", "theta", "vega", "gamma"):
        value = _optional_float(raw.get(greek))
        if value is not None and value >= 0:
            limits[greek] = value
    return limits


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
