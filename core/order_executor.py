"""Order payload creation and local position persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.models import OrderLeg, PositionSnapshot, StrategyOrder
from core.trade_log import append_trade_rows


def _net_limit_price(order: StrategyOrder) -> float:
    total = sum(leg.contract.mid_price() * leg.qty for leg in order.legs)
    if not order.legs:
        return 0.0
    return round(total / max(1, len(order.legs)), 2)


def build_order_payload(order: StrategyOrder) -> dict[str, Any]:
    if len(order.legs) == 1:
        leg = order.legs[0]
        return {
            "symbol": leg.contract.contract_symbol,
            "qty": leg.qty,
            "side": leg.side.replace("_to_open", ""),
            "type": "limit",
            "limit_price": round(leg.contract.mid_price(), 2),
            "time_in_force": "day",
        }
    return {
        "order_class": "mleg",
        "type": "limit",
        "limit_price": _net_limit_price(order),
        "time_in_force": "day",
        "legs": [
            {
                "symbol": leg.contract.contract_symbol,
                "ratio_qty": leg.qty,
                "side": leg.side.replace("_to_open", ""),
            }
            for leg in order.legs
        ],
    }


def execute_order(client: Any, order: StrategyOrder, *, dry_run: bool = True) -> dict[str, Any]:
    payload = build_order_payload(order)
    if dry_run:
        return {
            "id": f"dryrun-{order.strategy_id}",
            "status": "accepted",
            "payload": payload,
        }
    return client.submit_order(payload)


def trade_log_rows_from_order(
    order: StrategyOrder,
    *,
    mode: str,
    order_id: str,
    timestamp: datetime | None = None,
) -> list[dict[str, object]]:
    timestamp = timestamp or datetime.now(timezone.utc)
    rows: list[dict[str, object]] = []
    stop_loss = round(order.net_opening_credit * order.loss_stop_multiple, 2)
    take_profit = round(order.net_opening_credit * order.profit_take_pct, 2)
    for index, leg in enumerate(order.legs, start=1):
        contract = leg.contract
        rows.append(
            {
                "timestamp": timestamp.isoformat(timespec="seconds"),
                "mode": mode,
                "strategy": order.strategy_name,
                "underlying": order.underlying,
                "strategy_id": order.strategy_id,
                "leg_number": index,
                "contract_symbol": contract.contract_symbol,
                "option_type": contract.option_type,
                "strike": contract.strike,
                "expiration": contract.expiration.isoformat(),
                "dte_at_entry": contract.days_to_expiration(timestamp.date()),
                "side": leg.side,
                "qty": leg.qty,
                "entry_price": contract.mid_price(),
                "exit_price": "",
                "credit_received_per_spread": order.net_opening_credit,
                "max_loss_per_spread": order.max_loss,
                "stop_loss": stop_loss,
                "take_profit": take_profit,
                "pnl_pct": "",
                "exit_reason": "",
                "order_id": order_id,
                "status": "open",
                "delta_at_entry": contract.delta if contract.delta is not None else "",
                "theta_at_entry": contract.theta if contract.theta is not None else "",
                "vega_at_entry": contract.vega if contract.vega is not None else "",
                "iv_at_entry": contract.implied_volatility,
                "iv_rank_at_entry": order.iv_rank,
                "underlying_price_at_entry": contract.underlying_price,
            }
        )
    return rows


def position_from_order(order: StrategyOrder, *, opened_at: datetime | None = None) -> PositionSnapshot:
    opened_at = opened_at or datetime.now(timezone.utc)
    return PositionSnapshot(
        strategy_id=order.strategy_id,
        strategy_name=order.strategy_name,
        underlying=order.underlying,
        legs=[
            OrderLeg(contract=leg.contract, side=leg.side, qty=leg.qty)
            for leg in order.legs
        ],
        opened_at=opened_at,
        entry_credit=order.net_opening_credit,
        max_loss=order.max_loss,
        profit_take_pct=order.profit_take_pct,
        loss_stop_multiple=order.loss_stop_multiple,
        roll_threshold_delta=order.roll_threshold_delta,
        next_earnings_date=order.next_earnings_date,
        ex_dividend_date=order.ex_dividend_date,
    )


def load_positions(path: Path) -> list[PositionSnapshot]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        return []
    return [PositionSnapshot.from_dict(item) for item in payload if isinstance(item, dict)]


def save_positions(path: Path, positions: list[PositionSnapshot]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump([position.as_dict() for position in positions], handle, indent=2)


def persist_open_order(
    *,
    order: StrategyOrder,
    mode: str,
    order_id: str,
    trade_log_path: Path,
    positions_path: Path,
    timestamp: datetime | None = None,
) -> PositionSnapshot:
    timestamp = timestamp or datetime.now(timezone.utc)
    append_trade_rows(trade_log_path, trade_log_rows_from_order(order, mode=mode, order_id=order_id, timestamp=timestamp))
    positions = load_positions(positions_path)
    position = position_from_order(order, opened_at=timestamp)
    positions.append(position)
    save_positions(positions_path, positions)
    return position
