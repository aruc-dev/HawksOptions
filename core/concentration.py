"""Optional sector and correlation-group concentration checks."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from core.models import PositionSnapshot, StrategyOrder


def concentration_limit_reasons(
    order: StrategyOrder,
    *,
    open_positions: Iterable[PositionSnapshot],
    account: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    concentration_cfg = config.get("portfolio_concentration") or {}
    if not isinstance(concentration_cfg, dict):
        return []
    equity = _equity(account)
    if equity <= 0:
        return []
    reasons = []
    incoming_risk = max(float(order.max_loss), 0.0)
    metadata = _metadata_by_symbol(config)
    order_metadata = _symbol_metadata(order.underlying, metadata, order.metadata.get("underlying"))
    sector = _normalized_group_value(order_metadata.get("sector"))
    correlation_group = _normalized_group_value(order_metadata.get("correlation_group"))

    if sector:
        cap_pct = _cap_for(concentration_cfg, "sector", sector)
        if cap_pct is not None:
            usage = _risk_by_key(open_positions, metadata, "sector")
            if usage[sector] + incoming_risk > cap_pct * equity:
                reasons.append("sector_concentration_cap_exceeded")

    if correlation_group:
        cap_pct = _cap_for(concentration_cfg, "correlation_group", correlation_group)
        if cap_pct is not None:
            usage = _risk_by_key(open_positions, metadata, "correlation_group")
            if usage[correlation_group] + incoming_risk > cap_pct * equity:
                reasons.append("correlation_group_concentration_cap_exceeded")
    return reasons


def _metadata_by_symbol(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw = config.get("_underlying_metadata") or config.get("underlying_metadata") or {}
    if not isinstance(raw, dict):
        return {}
    return {str(symbol): item for symbol, item in raw.items() if isinstance(item, dict)}


def _symbol_metadata(
    symbol: str,
    metadata: dict[str, dict[str, Any]],
    order_metadata: Any = None,
) -> dict[str, Any]:
    merged = dict(metadata.get(symbol, {}))
    if isinstance(order_metadata, dict):
        merged.update(order_metadata)
    return merged


def _risk_by_key(
    positions: Iterable[PositionSnapshot],
    metadata: dict[str, dict[str, Any]],
    key: str,
) -> dict[str, float]:
    usage: dict[str, float] = defaultdict(float)
    for position in positions:
        value = _normalized_group_value(metadata.get(position.underlying, {}).get(key))
        if value:
            usage[value] += max(float(position.max_loss), 0.0)
    return usage


def _normalized_group_value(value: Any) -> str:
    return str(value or "").strip().lower()


def _cap_for(config: dict[str, Any], key: str, value: str) -> float | None:
    specific = config.get(f"{key}_caps_pct")
    if isinstance(specific, dict) and value in specific:
        return _non_negative_float(specific[value])
    if isinstance(specific, dict):
        for cap_key, cap_value in specific.items():
            if _normalized_group_value(cap_key) == value:
                return _non_negative_float(cap_value)
    return _non_negative_float(config.get(f"max_{key}_allocation_pct"))


def _non_negative_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _equity(account: dict[str, Any]) -> float:
    for key in ("equity", "portfolio_value"):
        value = _non_negative_float(account.get(key))
        if value is not None and value > 0:
            return value
    return 0.0
