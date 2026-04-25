"""Deterministic structural-trade critic."""

from __future__ import annotations

from core.models import StrategyOrder


def critique_trade(order: StrategyOrder) -> dict[str, object]:
    concerns = []
    if order.max_loss <= 0:
        concerns.append("max_loss is not positive")
    if not order.legs:
        concerns.append("order has no legs")
    if order.net_opening_credit == 0:
        concerns.append("net premium is zero")
    severity = "major" if concerns else "none"
    return {"concerns": concerns, "severity": severity}
