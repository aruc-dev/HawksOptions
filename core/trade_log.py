"""Trade-log helpers for the extended options schema."""

from __future__ import annotations

import csv
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Iterable

from core.file_lock import locked_open


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
]


def read_trade_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with locked_open(path, "r", lock="shared", newline="") as handle:
        return list(csv.DictReader(handle))


def append_trade_rows(path: Path, rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Lock file before checking existence so a concurrent writer cannot
    # create the file between our exists() check and our append, which
    # would result in a duplicate header.
    with locked_open(path, "a", lock="exclusive", newline="") as handle:
        # ``a`` mode + flock means we are the sole writer for the
        # duration of this block. Detect a header by seeking to start
        # and peeking; tell() == 0 means the file is empty.
        needs_header = handle.tell() == 0
        writer = csv.DictWriter(handle, fieldnames=TRADE_LOG_FIELDS)
        if needs_header:
            writer.writeheader()
        for row in rows:
            payload = {field: row.get(field, "") for field in TRADE_LOG_FIELDS}
            writer.writerow(payload)


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
