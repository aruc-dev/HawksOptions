"""Execution-quality summaries for option orders."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.models import StrategyOrder


def execution_quality_summary(
    order: StrategyOrder,
    *,
    response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response = response or {}
    legs_payload = response.get("legs") if isinstance(response.get("legs"), list) else []
    response_legs = {
        str(item.get("symbol", item.get("contract_symbol", ""))): item
        for item in legs_payload
        if isinstance(item, dict)
    }
    leg_summaries = []
    expected_net = 0.0
    actual_net = 0.0
    complete_actual_fill = True
    partial_fill = False
    for index, leg in enumerate(order.legs, start=1):
        expected_price = leg.contract.mid_price()
        leg_response = response_legs.get(leg.contract.contract_symbol, {})
        actual_price = _actual_leg_price(leg_response, expected_price if _simulated_fill(response) else None)
        filled_qty = _filled_qty(leg_response, leg.qty if _simulated_fill(response) else None)
        if filled_qty is not None and filled_qty < leg.qty:
            partial_fill = True
        if actual_price is None or filled_qty is None or filled_qty < leg.qty:
            complete_actual_fill = False
        expected_cashflow = leg.opening_cashflow()
        actual_cashflow = None
        slippage_per_share = None
        slippage_dollars = None
        if actual_price is not None:
            sign = 1.0 if leg.side == "sell_to_open" else -1.0
            qty = filled_qty if filled_qty is not None else leg.qty
            actual_cashflow = round(sign * actual_price * 100.0 * qty, 2)
            slippage_per_share = _slippage_per_share(leg.side, expected_price, actual_price)
            slippage_dollars = round(slippage_per_share * 100.0 * qty, 2)
            actual_net += actual_cashflow
        expected_net += expected_cashflow
        leg_summaries.append(
            {
                "leg_number": index,
                "contract_symbol": leg.contract.contract_symbol,
                "side": leg.side,
                "qty": leg.qty,
                "filled_qty": filled_qty,
                "expected_price": expected_price,
                "actual_price": actual_price,
                "expected_cashflow": expected_cashflow,
                "actual_cashflow": actual_cashflow,
                "slippage_per_share": slippage_per_share,
                "slippage_dollars": slippage_dollars,
            }
        )
    actual_net_value = round(actual_net, 2) if complete_actual_fill else None
    expected_net = round(expected_net, 2)
    return {
        "expected_net_opening_credit": expected_net,
        "actual_net_opening_credit": actual_net_value,
        "net_slippage_dollars": round(expected_net - actual_net_value, 2) if actual_net_value is not None else None,
        "order_duration_seconds": _duration_seconds(response),
        "partial_fill": partial_fill or str(response.get("status", "")).lower() == "partially_filled",
        "retry_count": int(response.get("retry_count", response.get("retries", 0)) or 0),
        "legs": leg_summaries,
    }


def _simulated_fill(response: dict[str, Any]) -> bool:
    return bool(response.get("simulated_fill", False))


def _actual_leg_price(leg_response: dict[str, Any], default: float | None) -> float | None:
    for key in ("filled_avg_price", "avg_fill_price", "actual_price", "fill_price"):
        if key in leg_response and leg_response[key] not in (None, ""):
            return round(float(leg_response[key]), 4)
    return default


def _filled_qty(leg_response: dict[str, Any], default: int | None) -> int | None:
    for key in ("filled_qty", "qty_filled"):
        if key in leg_response and leg_response[key] not in (None, ""):
            return int(float(leg_response[key]))
    return default


def _slippage_per_share(side: str, expected_price: float, actual_price: float) -> float:
    if side == "sell_to_open":
        return round(expected_price - actual_price, 4)
    return round(actual_price - expected_price, 4)


def _duration_seconds(response: dict[str, Any]) -> float | None:
    submitted = _parse_time(response.get("submitted_at") or response.get("created_at"))
    filled = _parse_time(response.get("filled_at") or response.get("updated_at"))
    if submitted is None or filled is None:
        return None
    return round(max(0.0, (filled - submitted).total_seconds()), 3)


def _parse_time(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None
