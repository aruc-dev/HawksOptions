"""Safe close-order planning for continuous risk actions."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import reduce
from math import gcd
from typing import Any, Iterable

from core.models import PositionSnapshot
from core.nbbo import capture_nbbo_snapshot, has_complete_client_nbbo


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

PENDING_CLOSE_TTL = timedelta(minutes=15)
ACTIVE_PENDING_CLOSE_STATUSES = {
    "accepted",
    "new",
    "pending_new",
    "submitted",
    "partially_filled",
    "pending_cancel",
    "pending_replace",
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
        pending_state = _pending_close_state(position, client)
        if pending_state in {"active", "closed"}:
            plans.append(
                {
                    "strategy_id": strategy_id,
                    "action": action_name,
                    "dry_run": True,
                    "execute_enabled": bool(execute_enabled),
                    "payload": {},
                    "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "result": {
                        "status": "skipped_closed" if pending_state == "closed" else "skipped_pending_close",
                        "order_id": position.pending_close_order_id or position.close_order_id,
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
            nbbo_snapshot = capture_nbbo_snapshot(client, position)
            plan["nbbo_snapshot"] = nbbo_snapshot
            if not has_complete_client_nbbo(nbbo_snapshot):
                raise RuntimeError("fresh_nbbo_required_for_live_close")
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
    if not quantities:
        return 1
    return max(1, reduce(gcd, quantities))


def _mark_pending_close(position: PositionSnapshot, action_name: str, result: Any) -> None:
    if not isinstance(result, dict):
        return
    status = str(result.get("status", "")).lower()
    order_id = str(result.get("id") or result.get("order_id") or "")
    if status == "filled" and order_id:
        _mark_closed(position, order_id=order_id, action_name=action_name)
        return
    if status not in ACTIVE_PENDING_CLOSE_STATUSES or not order_id:
        return
    position.pending_close_order_id = order_id
    position.pending_close_action = action_name
    position.pending_close_submitted_at = datetime.now(timezone.utc)


def _pending_close_state(position: PositionSnapshot, client: Any) -> str:
    if not position.pending_close_order_id:
        return "inactive"
    status = _pending_close_status(position.pending_close_order_id, client)
    if status == "filled":
        _mark_closed(position, order_id=position.pending_close_order_id, action_name=position.pending_close_action)
        return "closed"
    if status == "__lookup_failed__":
        return "active"
    if status in {"canceled", "cancelled", "rejected", "expired"}:
        _clear_pending_close(position)
        return "inactive"
    if status in ACTIVE_PENDING_CLOSE_STATUSES:
        return "active"
    if _pending_close_is_recent(position):
        return "active"
    _clear_pending_close(position)
    return "inactive"


def _pending_close_status(order_id: str, client: Any) -> str:
    has_status_getter = False
    for method_name in ("get_order_status", "get_order"):
        getter = getattr(client, method_name, None)
        if not callable(getter):
            continue
        has_status_getter = True
        try:
            raw = getter(order_id)
        except Exception:
            return "__lookup_failed__"
        if isinstance(raw, str):
            return raw.lower()
        if isinstance(raw, dict):
            return str(raw.get("status", "")).lower()
        status = getattr(raw, "status", "")
        if status:
            return str(status).lower()
    if not has_status_getter:
        return "__lookup_failed__"
    return ""


def _pending_close_is_recent(position: PositionSnapshot) -> bool:
    submitted_at = position.pending_close_submitted_at
    if submitted_at is None:
        return False
    if submitted_at.tzinfo is None:
        submitted_at = submitted_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - submitted_at < PENDING_CLOSE_TTL


def _clear_pending_close(position: PositionSnapshot) -> None:
    position.pending_close_order_id = ""
    position.pending_close_action = ""
    position.pending_close_submitted_at = None


def _mark_closed(position: PositionSnapshot, *, order_id: str, action_name: str) -> None:
    position.closed_at = datetime.now(timezone.utc)
    position.close_order_id = order_id
    position.close_action = action_name
    _clear_pending_close(position)


def _closing_leg_payload(leg) -> dict[str, Any]:
    return {
        "symbol": leg.contract.contract_symbol,
        "qty": leg.qty,
        "ratio_qty": leg.qty,
        "side": _closing_side(leg.side),
    }


def _closing_side(open_side: str) -> str:
    if open_side == "sell_to_open":
        return "buy"
    if open_side == "buy_to_open":
        return "sell"
    raise ValueError(f"unsupported open leg side {open_side!r}")
