"""Safe close-order planning for continuous risk actions."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from core.models import PositionSnapshot


CLOSE_ACTIONS = {
    "take_profit",
    "stop_loss",
    "time_exit",
    "close_before_earnings",
    "close_for_ex_div",
    "close_for_calendar_assignment",
}


def build_close_order_payload(position: PositionSnapshot) -> dict[str, Any]:
    legs = [_closing_leg_payload(leg) for leg in position.legs]
    net_close_cashflow = sum(leg.closing_cashflow() for leg in position.legs)
    limit_price = round(abs(net_close_cashflow) / 100.0, 2)
    if len(legs) == 1:
        return {
            **legs[0],
            "type": "limit",
            "limit_price": limit_price,
            "time_in_force": "day",
            "position_intent": "close",
        }
    return {
        "order_class": "mleg",
        "type": "limit",
        "limit_price": limit_price,
        "time_in_force": "day",
        "position_intent": "close",
        "legs": legs,
    }


def close_order_plans(
    positions: Iterable[PositionSnapshot],
    actions: Iterable[dict[str, Any]],
    *,
    client: Any,
    execute_enabled: bool,
    dry_run: bool,
) -> list[dict[str, Any]]:
    positions_by_id = {position.strategy_id: position for position in positions}
    plans = []
    for action in actions:
        action_name = str(action.get("action", ""))
        strategy_id = str(action.get("strategy_id", ""))
        if action_name not in CLOSE_ACTIONS or strategy_id not in positions_by_id:
            continue
        payload = build_close_order_payload(positions_by_id[strategy_id])
        plan = {
            "strategy_id": strategy_id,
            "action": action_name,
            "dry_run": bool(dry_run or not execute_enabled),
            "execute_enabled": bool(execute_enabled),
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if execute_enabled and not dry_run:
            plan["result"] = client.submit_order(payload)
        else:
            plan["result"] = {"status": "planned"}
        plans.append(plan)
    return plans


def _closing_leg_payload(leg) -> dict[str, Any]:
    return {
        "symbol": leg.contract.contract_symbol,
        "qty": leg.qty,
        "ratio_qty": leg.qty,
        "side": _closing_side(leg.side),
    }


def _closing_side(open_side: str) -> str:
    if open_side == "sell_to_open":
        return "buy_to_close"
    if open_side == "buy_to_open":
        return "sell_to_close"
    raise ValueError(f"unsupported open leg side {open_side!r}")
