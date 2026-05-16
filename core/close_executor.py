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

ACTION_PRIORITY = {
    "stop_loss": 0,
    "close_before_earnings": 1,
    "close_for_ex_div": 2,
    "close_for_calendar_assignment": 3,
    "take_profit": 4,
    "time_exit": 5,
}


def build_close_order_payload(position: PositionSnapshot) -> dict[str, Any]:
    legs = [_closing_leg_payload(leg) for leg in position.legs]
    net_close_cashflow = sum(leg.closing_cashflow() for leg in position.legs)
    limit_price = round(abs(net_close_cashflow) / (100.0 * _position_unit_quantity(position)), 2)
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
    for action in _dedupe_close_actions(actions, positions_by_id):
        action_name = str(action.get("action", ""))
        strategy_id = str(action.get("strategy_id", ""))
        if action_name not in CLOSE_ACTIONS or strategy_id not in positions_by_id:
            continue
        position = positions_by_id[strategy_id]
        if position.pending_close_order_id:
            plans.append(
                {
                    "strategy_id": strategy_id,
                    "action": action_name,
                    "dry_run": True,
                    "execute_enabled": bool(execute_enabled),
                    "payload": {},
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "result": {
                        "status": "skipped_pending_close",
                        "order_id": position.pending_close_order_id,
                    },
                }
            )
            continue
        payload = build_close_order_payload(position)
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
            _mark_pending_close(position, action_name, plan["result"])
        else:
            plan["result"] = {"status": "planned"}
        plans.append(plan)
    return plans


def _dedupe_close_actions(
    actions: Iterable[dict[str, Any]],
    positions_by_id: dict[str, PositionSnapshot],
) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for action in actions:
        action_name = str(action.get("action", ""))
        strategy_id = str(action.get("strategy_id", ""))
        if action_name not in CLOSE_ACTIONS or strategy_id not in positions_by_id:
            continue
        current = selected.get(strategy_id)
        if current is None or ACTION_PRIORITY[action_name] < ACTION_PRIORITY[str(current.get("action", ""))]:
            selected[strategy_id] = action
    return list(selected.values())


def _position_unit_quantity(position: PositionSnapshot) -> int:
    quantities = [abs(int(leg.qty)) for leg in position.legs if int(leg.qty) != 0]
    return max(quantities, default=1)


def _mark_pending_close(position: PositionSnapshot, action_name: str, result: Any) -> None:
    if not isinstance(result, dict):
        return
    status = str(result.get("status", "")).lower()
    order_id = str(result.get("id") or result.get("order_id") or "")
    if status not in {"accepted", "new", "pending_new", "submitted"} or not order_id:
        return
    position.pending_close_order_id = order_id
    position.pending_close_action = action_name
    position.pending_close_submitted_at = datetime.now(timezone.utc)


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
