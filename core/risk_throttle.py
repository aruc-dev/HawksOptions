"""Optional drawdown-aware new-entry throttles."""

from __future__ import annotations

from typing import Any

from core.models import StrategyOrder


def risk_throttle_reasons(
    order: StrategyOrder,
    *,
    account: dict[str, Any],
    config: dict[str, Any],
) -> list[str]:
    throttle_cfg = config.get("risk_throttle") or {}
    if not isinstance(throttle_cfg, dict):
        return []
    equity = _equity(account)
    if equity <= 0:
        return []
    drawdown_pct = _drawdown_pct(account)
    daily_loss_pct = _optional_float(account.get("daily_loss_pct")) or 0.0
    reasons = []

    drawdown_halt = _optional_float(throttle_cfg.get("max_drawdown_halt_pct"))
    if drawdown_halt is not None and drawdown_pct >= drawdown_halt:
        reasons.append("drawdown_halt_new_entries")

    daily_halt = _optional_float(throttle_cfg.get("daily_loss_halt_pct"))
    if daily_halt is not None and daily_loss_pct >= daily_halt:
        reasons.append("daily_loss_halt_new_entries")

    reduce_threshold = _optional_float(throttle_cfg.get("reduce_risk_drawdown_pct"))
    throttled_risk_pct = _optional_float(throttle_cfg.get("max_throttled_position_risk_pct"))
    if (
        reduce_threshold is not None
        and throttled_risk_pct is not None
        and drawdown_pct >= reduce_threshold
        and float(order.max_loss) > throttled_risk_pct * equity
    ):
        reasons.append("drawdown_risk_throttle_exceeded")
    return reasons


def _drawdown_pct(account: dict[str, Any]) -> float:
    explicit = _optional_float(account.get("drawdown_pct"))
    if explicit is not None:
        return max(0.0, explicit)
    equity = _equity(account)
    peak_equity = _optional_float(account.get("peak_equity"))
    if equity <= 0 or peak_equity is None or peak_equity <= 0:
        return 0.0
    return max(0.0, (peak_equity - equity) / peak_equity)


def _equity(account: dict[str, Any]) -> float:
    for key in ("equity", "portfolio_value"):
        value = _optional_float(account.get(key))
        if value is not None and value > 0:
            return value
    return 0.0


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
