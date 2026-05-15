"""Portfolio allocation helpers for strategy-family exposure caps."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable

from core.models import PositionSnapshot, StrategyOrder
from core.strategy_types import LONG_PREMIUM_STRATEGIES, SHORT_PREMIUM_STRATEGIES


def strategy_families(strategy_name: str, *, net_credit: float | None = None) -> set[str]:
    families: set[str] = set()
    if strategy_name == "tail_risk_hedge":
        families.add("hedge")
    elif strategy_name == "collar":
        families.add("stock_hedge")
    elif strategy_name == "butterfly":
        families.add("short_premium" if (net_credit or 0.0) >= 0 else "long_premium")
    elif strategy_name in SHORT_PREMIUM_STRATEGIES:
        families.add("short_premium")
    elif strategy_name in LONG_PREMIUM_STRATEGIES:
        families.add("long_premium")
    else:
        families.add("other")
    if "earnings" in strategy_name or "volatility_crush" in strategy_name:
        families.add("earnings_event")
    return families


def allocation_limit_reasons(
    order: StrategyOrder,
    *,
    open_positions: Iterable[PositionSnapshot],
    account: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    allocation_cfg = config.get("portfolio_allocation") or {}
    if not isinstance(allocation_cfg, dict):
        return []
    family_caps = _float_mapping(allocation_cfg.get("family_caps_pct"))
    underlying_caps = _float_mapping(allocation_cfg.get("underlying_caps_pct"))
    single_underlying_cap = _optional_float(allocation_cfg.get("max_single_underlying_allocation_pct"))
    if not family_caps and not underlying_caps and single_underlying_cap is None:
        return []
    equity = _equity(account)
    if equity <= 0:
        return []

    positions = list(open_positions)
    incoming_risk = max(float(order.max_loss), 0.0)
    reasons: list[str] = []
    family_usage = allocation_by_family(positions)
    for family in strategy_families(order.strategy_name, net_credit=order.net_opening_credit):
        cap_pct = family_caps.get(family)
        if cap_pct is not None and family_usage[family] + incoming_risk > cap_pct * equity:
            reasons.append(f"portfolio_allocation_{family}_cap_exceeded")

    underlying_usage = allocation_by_underlying(positions)
    symbol = order.underlying
    cap_pct = underlying_caps.get(symbol, single_underlying_cap)
    if cap_pct is not None and underlying_usage[symbol] + incoming_risk > cap_pct * equity:
        reasons.append("underlying_allocation_cap_exceeded")
    return reasons


def allocation_by_family(positions: Iterable[PositionSnapshot]) -> dict[str, float]:
    usage: dict[str, float] = defaultdict(float)
    for position in positions:
        for family in strategy_families(position.strategy_name, net_credit=position.entry_credit):
            usage[family] += max(float(position.max_loss), 0.0)
    return dict(usage)


def allocation_by_underlying(positions: Iterable[PositionSnapshot]) -> dict[str, float]:
    usage: dict[str, float] = defaultdict(float)
    for position in positions:
        usage[position.underlying] += max(float(position.max_loss), 0.0)
    return dict(usage)


def _float_mapping(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    parsed = {}
    for key, item in value.items():
        number = _optional_float(item)
        if number is not None and number >= 0:
            parsed[str(key)] = number
    return parsed


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _equity(account: dict[str, Any]) -> float:
    for key in ("equity", "portfolio_value"):
        value = _optional_float(account.get(key))
        if value is not None and value > 0:
            return value
    return 0.0
