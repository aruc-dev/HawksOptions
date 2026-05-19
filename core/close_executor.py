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
    "accepted_for_bidding",
    "calculated",
    "done_for_day",
    "new",
    "pending_new",
    "submitted",
    "partially_filled",
    "pending_cancel",
    "pending_replace",
    "stopped",
    "suspended",
}


def build_close_order_payload(position: PositionSnapshot) -> dict[str, Any]:
    legs = [_closing_leg_payload(leg, include_qty=len(position.legs) == 1) for leg in position.legs]
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
    allowed_auto_close_actions: Iterable[str] | None = None,
) -> list[dict[str, Any]]:
    positions_by_id = {position.strategy_id: position for position in positions}
    plans = reconcile_pending_closes(positions_by_id.values(), client=client)
    allowed_actions = {str(item) for item in (allowed_auto_close_actions or CLOSE_ACTIONS)}
    for action in _dedupe_close_actions(actions, positions_by_id):
        action_name = str(action.get("action", ""))
        strategy_id = str(action.get("strategy_id", ""))
        if action_name not in CLOSE_ACTIONS or strategy_id not in positions_by_id:
            continue
        position = positions_by_id[strategy_id]
        if position.closed_at is not None:
            continue
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
            "dry_run": bool(dry_run or not execute_enabled or action_name not in allowed_actions),
            "execute_enabled": bool(execute_enabled),
            "auto_close_allowed": action_name in allowed_actions,
            "payload": payload,
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        if execute_enabled and not dry_run and action_name in allowed_actions:
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


def reconcile_pending_closes(
    positions: Iterable[PositionSnapshot],
    *,
    client: Any,
) -> list[dict[str, Any]]:
    plans = []
    for position in positions:
        if not position.pending_close_order_id:
            continue
        order_id = position.pending_close_order_id
        action_name = position.pending_close_action
        state = _pending_close_state(position, client)
        if state != "closed":
            continue
        plans.append(
            {
                "strategy_id": position.strategy_id,
                "action": action_name,
                "dry_run": True,
                "execute_enabled": False,
                "payload": {},
                "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "result": {
                    "status": "reconciled_closed",
                    "order_id": position.close_order_id or order_id,
                },
            }
        )
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
        if not _mark_closed(position, order_id=order_id, action_name=action_name, result=result):
            position.pending_close_order_id = order_id
            position.pending_close_action = action_name
            position.pending_close_submitted_at = datetime.now(timezone.utc)
        return
    if status not in ACTIVE_PENDING_CLOSE_STATUSES or not order_id:
        return
    position.pending_close_order_id = order_id
    position.pending_close_action = action_name
    position.pending_close_submitted_at = datetime.now(timezone.utc)


def _pending_close_state(position: PositionSnapshot, client: Any) -> str:
    if not position.pending_close_order_id:
        return "inactive"
    status, result = _pending_close_status(position.pending_close_order_id, client)
    if status == "filled":
        if not _mark_closed(
            position,
            order_id=position.pending_close_order_id,
            action_name=position.pending_close_action,
            result=result,
        ):
            return "active"
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


def _pending_close_status(order_id: str, client: Any) -> tuple[str, Any]:
    has_status_getter = False
    for method_name in ("get_order_status", "get_order"):
        getter = getattr(client, method_name, None)
        if not callable(getter):
            continue
        has_status_getter = True
        try:
            raw = getter(order_id)
        except Exception:
            return "__lookup_failed__", None
        if isinstance(raw, str):
            if raw:
                return raw.lower(), raw
            continue
        if isinstance(raw, dict):
            status = str(raw.get("status", "")).lower()
            if status:
                return status, raw
            continue
        status = getattr(raw, "status", "")
        if status:
            return str(status).lower(), raw
    if not has_status_getter:
        return "__lookup_failed__", None
    return "", None


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


def _mark_closed(position: PositionSnapshot, *, order_id: str, action_name: str, result: Any = None) -> bool:
    close_fill_prices = _close_fill_prices(result)
    expected_symbols = {leg.contract.contract_symbol for leg in position.legs}
    if not expected_symbols.issubset(close_fill_prices):
        return False
    position.closed_at = datetime.now(timezone.utc)
    position.close_order_id = order_id
    position.close_action = action_name
    position.close_fill_prices = close_fill_prices
    _clear_pending_close(position)
    return True


def _close_fill_prices(result: Any) -> dict[str, float]:
    if result is None:
        return {}
    legs = result.get("legs") if isinstance(result, dict) else getattr(result, "legs", None)
    if not isinstance(legs, list):
        return {}
    out: dict[str, float] = {}
    for leg in legs:
        symbol = str(leg.get("symbol") or leg.get("contract_symbol") or "") if isinstance(leg, dict) else str(getattr(leg, "symbol", "") or getattr(leg, "contract_symbol", ""))
        raw_price = leg.get("filled_avg_price", leg.get("avg_price")) if isinstance(leg, dict) else getattr(leg, "filled_avg_price", getattr(leg, "avg_price", None))
        try:
            price = float(raw_price)
        except (TypeError, ValueError):
            continue
        if symbol and price > 0:
            out[symbol] = price
    return out


def _closing_leg_payload(leg, *, include_qty: bool) -> dict[str, Any]:
    payload = {
        "symbol": leg.contract.contract_symbol,
        "ratio_qty": leg.qty,
        "side": _closing_side(leg.side),
    }
    if include_qty:
        payload["qty"] = leg.qty
    return payload


def _closing_side(open_side: str) -> str:
    if open_side == "sell_to_open":
        return "buy"
    if open_side == "buy_to_open":
        return "sell"
    raise ValueError(f"unsupported open leg side {open_side!r}")
