"""Roll decisions for options positions."""

from __future__ import annotations

from dataclasses import dataclass

from core.models import PositionSnapshot, StrategyOrder


@dataclass(frozen=True)
class RollDecision:
    should_roll: bool
    reason: str


def should_roll_position(
    position: PositionSnapshot,
    *,
    max_rolls: int = 2,
) -> RollDecision:
    if position.roll_count >= max_rolls:
        return RollDecision(False, "max_rolls_reached")
    if position.roll_threshold_delta is None:
        return RollDecision(False, "strategy_has_no_roll_rule")
    if abs(position.short_delta) >= abs(position.roll_threshold_delta):
        return RollDecision(True, "delta_threshold_breached")
    if position.days_to_expiration <= 21:
        return RollDecision(True, "time_exit_window")
    return RollDecision(False, "within_tolerance")


def build_roll_plan(
    current_position: PositionSnapshot,
    replacement_order: StrategyOrder,
) -> dict[str, object] | None:
    net_credit = replacement_order.net_opening_credit - current_position.current_close_cost
    if net_credit <= 0:
        return None
    return {
        "strategy_id": current_position.strategy_id,
        "underlying": current_position.underlying,
        "close_reason": "roll_out",
        "replacement_strategy_id": replacement_order.strategy_id,
        "net_credit": round(net_credit, 2),
    }
