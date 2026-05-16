"""Explicit event-risk context and optional strategy gates."""

from __future__ import annotations

from typing import Any


def event_risk_context(underlying: dict[str, Any]) -> dict[str, Any]:
    flagged = bool(underlying.get("event_risk", False))
    level = _event_level(underlying.get("event_risk_level"))
    if level > 0:
        flagged = True
    return {
        "event_risk": flagged,
        "event_risk_level": level,
        "event_risk_reason": str(underlying.get("event_risk_reason", "")),
        "event_risk_source": str(underlying.get("event_risk_source", "underlying_metadata" if flagged else "unavailable")),
    }


def event_risk_passes(underlying: dict[str, Any], params: dict[str, Any]) -> bool:
    context = event_risk_context(underlying)
    if bool(params.get("block_event_risk", False)) and bool(context["event_risk"]):
        return False
    if "max_event_risk_level" in params and float(context["event_risk_level"]) > float(params["max_event_risk_level"]):
        return False
    return True


def _event_level(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    if isinstance(value, str):
        mapped = {"low": 0.25, "medium": 0.5, "high": 0.75, "extreme": 1.0}
        lowered = value.lower()
        if lowered in mapped:
            return mapped[lowered]
    try:
        return max(0.0, min(float(value), 1.0))
    except (TypeError, ValueError):
        return 0.0
