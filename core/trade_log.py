"""Trade-log helpers for the extended options schema."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from core.file_lock import locked_open
from core.models import PositionSnapshot


TRADE_LOG_FIELDS = [
    "timestamp",
    "mode",
    "strategy",
    "underlying",
    "strategy_id",
    "leg_number",
    "contract_symbol",
    "option_type",
    "strike",
    "expiration",
    "dte_at_entry",
    "side",
    "qty",
    "entry_price",
    "exit_price",
    "credit_received_per_spread",
    "max_loss_per_spread",
    "stop_loss",
    "take_profit",
    "pnl_pct",
    "exit_reason",
    "order_id",
    "status",
    "delta_at_entry",
    "theta_at_entry",
    "vega_at_entry",
    "iv_at_entry",
    "iv_rank_at_entry",
    "underlying_price_at_entry",
    "expected_entry_price",
    "actual_entry_price",
    "leg_slippage_dollars",
    "order_duration_seconds",
    "partial_fill",
    "retry_count",
    "close_timestamp",
]


def read_trade_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with locked_open(path, "r", lock="shared", newline="") as handle:
        return list(csv.DictReader(handle))


def append_trade_rows(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked_open(path, "a", lock="exclusive", newline="") as handle:
        # The stream can be opened before another process writes the
        # header, so inspect the on-disk size after acquiring the lock.
        try:
            needs_header = path.stat().st_size == 0
        except FileNotFoundError:
            needs_header = True
        writer = csv.DictWriter(handle, fieldnames=TRADE_LOG_FIELDS)
        if needs_header:
            writer.writeheader()
        for row in rows:
            payload = {field: row.get(field, "") for field in TRADE_LOG_FIELDS}
            writer.writerow(payload)


def mark_strategy_closed(
    path: Path,
    position: PositionSnapshot,
    *,
    exit_reason: str,
    closed_at: datetime | None = None,
) -> int:
    """Mark persisted trade-log rows for a filled close as closed.

    The trade log is leg-based. Updating all open rows for the strategy keeps
    dashboard and drift-report readers aligned with positions.json when a close
    fill has been reconciled.
    """
    if not path.exists():
        return 0
    closed_at = closed_at or position.closed_at or datetime.now(timezone.utc)
    if closed_at.tzinfo is None:
        closed_at = closed_at.replace(tzinfo=timezone.utc)
    exit_prices = {
        leg.contract.contract_symbol: round(float(leg.contract.mid_price()), 4)
        for leg in position.legs
    }
    exit_prices.update(position.close_fill_prices)
    updated_rows = []
    updated = 0
    with locked_open(path, "r+", lock="exclusive", newline="") as handle:
        rows = list(csv.DictReader(handle))
        for row in rows:
            if str(row.get("strategy_id", "")) != position.strategy_id:
                continue
            if str(row.get("status", "")).lower() not in {"open", "partially_filled"}:
                continue
            symbol = str(row.get("contract_symbol", ""))
            row["close_timestamp"] = closed_at.isoformat(timespec="seconds")
            row["exit_price"] = _csv_text(exit_prices.get(symbol, ""))
            row["exit_reason"] = exit_reason
            row["order_id"] = position.close_order_id or row.get("order_id", "")
            row["status"] = "closed"
            updated_rows.append(row)
            updated += 1
        pnl_pct = _realized_pnl_pct(updated_rows, position)
        for row in updated_rows:
            row["pnl_pct"] = _csv_text(pnl_pct)
        if updated:
            handle.seek(0)
            handle.truncate(0)
            writer = csv.DictWriter(handle, fieldnames=TRADE_LOG_FIELDS)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in TRADE_LOG_FIELDS})
    return updated


def _realized_pnl_pct(rows: Iterable[dict[str, str]], position: PositionSnapshot) -> float | str:
    basis = abs(float(position.entry_credit or 0.0))
    if basis <= 0:
        basis = abs(float(position.max_loss or 0.0))
    if basis <= 0:
        return ""
    total = 0.0
    saw_leg = False
    for row in rows:
        try:
            entry = float(row.get("entry_price", ""))
            exit_price = float(row.get("exit_price", ""))
            qty = float(row.get("qty", 1) or 1)
        except (TypeError, ValueError):
            continue
        if entry <= 0 or exit_price <= 0 or qty <= 0:
            continue
        side = str(row.get("side", "")).lower()
        if side.startswith("sell"):
            leg_pnl = (entry - exit_price) * qty * 100.0
        else:
            leg_pnl = (exit_price - entry) * qty * 100.0
        total += leg_pnl
        saw_leg = True
    if not saw_leg:
        return _position_pnl_pct(position)
    return round((total / basis) * 100.0, 4)


def _position_pnl_pct(position: PositionSnapshot) -> float | str:
    basis = abs(float(position.entry_credit or 0.0))
    if basis <= 0:
        basis = abs(float(position.max_loss or 0.0))
    if basis <= 0:
        return ""
    return round((float(position.current_pnl) / basis) * 100.0, 4)


def _csv_text(value: object) -> object:
    return "" if value is None else value


def open_strategy_rows(rows: Iterable[dict[str, str]]) -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        if str(row.get("status", "")).lower() not in {"open", "partially_filled"}:
            continue
        groups[str(row.get("strategy_id", ""))].append(row)
    return dict(groups)


def closed_strategy_rows(rows: Iterable[dict[str, str]]) -> list[dict[str, str]]:
    return [
        row
        for row in rows
        if str(row.get("status", "")).lower() == "closed"
    ]


def latest_trade_timestamp(rows: Iterable[dict[str, str]]) -> datetime | None:
    timestamps = []
    for row in rows:
        text = str(row.get("timestamp", "")).strip()
        if not text:
            continue
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            timestamps.append(datetime.fromisoformat(text))
        except ValueError:
            continue
    return max(timestamps) if timestamps else None
