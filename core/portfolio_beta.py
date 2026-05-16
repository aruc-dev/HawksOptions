"""SPY-beta-weighted portfolio exposure checks."""

from __future__ import annotations

from typing import Any, Iterable

from core.models import OrderLeg, PositionSnapshot, StrategyOrder


def order_spy_beta_delta(order: StrategyOrder, config: dict[str, Any]) -> float:
    return round(sum(_leg_spy_beta_delta(leg, config) for leg in order.legs), 4)


def position_spy_beta_delta(position: PositionSnapshot, config: dict[str, Any]) -> float:
    return round(sum(_leg_spy_beta_delta(leg, config) for leg in position.legs), 4)


def aggregate_spy_beta_delta(positions: Iterable[PositionSnapshot], config: dict[str, Any]) -> float:
    return round(sum(position_spy_beta_delta(position, config) for position in positions), 4)


def beta_limit_reasons(
    order: StrategyOrder,
    *,
    open_positions: Iterable[PositionSnapshot],
    account: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    beta_cfg = config.get("portfolio_beta_limits") or {}
    if not isinstance(beta_cfg, dict) or not bool(beta_cfg.get("enabled", False)):
        return []
    max_pct = _optional_float(beta_cfg.get("max_abs_spy_beta_delta_pct"))
    equity = _optional_float(account.get("portfolio_value") or account.get("equity"))
    if max_pct is None or max_pct < 0 or equity is None or equity <= 0:
        return []
    projected = aggregate_spy_beta_delta(open_positions, config) + order_spy_beta_delta(order, config)
    if abs(projected) > equity * max_pct:
        return ["portfolio_spy_beta_delta_limit_exceeded"]
    return []


def _leg_spy_beta_delta(leg: OrderLeg, config: dict[str, Any]) -> float:
    contract = leg.contract
    delta = float(contract.delta or 0.0)
    price = float(contract.underlying_price or 0.0)
    beta = _beta_to_spy(contract.underlying, config)
    sign = -1.0 if leg.side == "sell_to_open" else 1.0
    return sign * delta * 100.0 * max(1, int(leg.qty)) * price * beta


def _beta_to_spy(symbol: str, config: dict[str, Any]) -> float:
    beta_cfg = config.get("portfolio_beta_limits") or {}
    configured = beta_cfg.get("symbol_betas", {}) if isinstance(beta_cfg, dict) else {}
    if isinstance(configured, dict):
        value = _optional_float(configured.get(symbol))
        if value is not None and value >= 0:
            return value
    metadata = config.get("_underlying_metadata", {})
    if isinstance(metadata, dict):
        value = _optional_float((metadata.get(symbol) or {}).get("beta_to_spy"))
        if value is not None and value >= 0:
            return value
    return 1.0


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
