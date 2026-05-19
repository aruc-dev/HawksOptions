"""Broker/local state reconciliation."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.file_lock import atomic_write_text
from core.models import OptionContract, OrderLeg, PositionSnapshot
from core.occ import parse_occ_symbol
from core.order_executor import load_positions, save_positions
from core.runtime_guard import write_halt_file


def reconcile_state(
    *,
    client: Any,
    positions_path: Path,
    reports_dir: Path,
    halt_file: Path,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc)
    local_positions = load_positions(positions_path)
    broker_positions = _broker_option_positions(client)
    local_qty = _local_contract_qty(local_positions)
    broker_qty = _broker_contract_qty(broker_positions)
    missing_local = sorted(symbol for symbol in broker_qty if symbol not in local_qty)
    orphan_local = sorted(symbol for symbol in local_qty if symbol not in broker_qty)
    mismatched_qty = sorted(
        symbol
        for symbol in broker_qty.keys() & local_qty.keys()
        if broker_qty[symbol] != local_qty[symbol]
    )
    report = {
        "generated_at": as_of.isoformat(timespec="seconds"),
        "missing_local": missing_local,
        "orphan_local": orphan_local,
        "mismatched_qty": mismatched_qty,
        "halted": bool(mismatched_qty),
    }
    if missing_local or orphan_local:
        local_positions = _apply_nonfatal_reconciliation(local_positions, broker_positions, missing_local, orphan_local, as_of=as_of)
        save_positions(positions_path, local_positions)
    if mismatched_qty:
        write_halt_file(halt_file, reason="reconciliation_mismatched_qty:" + ",".join(mismatched_qty))
    report_path = _write_reconciliation_report(reports_dir, report, as_of=as_of)
    report["report_path"] = str(report_path)
    return report


def _broker_option_positions(client: Any) -> list[dict[str, Any]]:
    positions = client.get_positions()
    out = []
    for position in positions:
        symbol = str(position.get("symbol", ""))
        try:
            parse_occ_symbol(symbol)
        except ValueError:
            continue
        qty = _int_qty(position.get("qty"))
        if qty == 0:
            continue
        out.append(position)
    return out


def _local_contract_qty(positions: list[PositionSnapshot]) -> dict[str, int]:
    qty: dict[str, int] = defaultdict(int)
    for position in positions:
        if position.closed_at is not None:
            continue
        for leg in position.legs:
            sign = -1 if leg.side == "sell_to_open" else 1
            qty[leg.contract.contract_symbol] += sign * int(leg.qty)
    return dict(qty)


def _broker_contract_qty(positions: list[dict[str, Any]]) -> dict[str, int]:
    return {str(position["symbol"]): _int_qty(position.get("qty")) for position in positions}


def _apply_nonfatal_reconciliation(
    local_positions: list[PositionSnapshot],
    broker_positions: list[dict[str, Any]],
    missing_local: list[str],
    orphan_local: list[str],
    *,
    as_of: datetime,
) -> list[PositionSnapshot]:
    orphan_set = set(orphan_local)
    out = [
        position
        for position in local_positions
        if not any(leg.contract.contract_symbol in orphan_set for leg in position.legs)
    ]
    for position in broker_positions:
        symbol = str(position["symbol"])
        if symbol not in missing_local:
            continue
        out.append(_position_from_broker_option(position, opened_at=as_of))
    return out


def _position_from_broker_option(position: dict[str, Any], *, opened_at: datetime) -> PositionSnapshot:
    symbol = str(position["symbol"])
    parsed = parse_occ_symbol(symbol)
    qty = abs(_int_qty(position.get("qty")))
    market_price = _float(position.get("current_price") or position.get("avg_entry_price"))
    avg_entry = _float(position.get("avg_entry_price") or market_price)
    contract = OptionContract(
        contract_symbol=symbol,
        underlying=str(parsed["underlying"]),
        option_type=str(parsed["option_type"]),
        strike=float(parsed["strike"]),
        expiration=parsed["expiration"],  # type: ignore[arg-type]
        bid=market_price,
        ask=market_price,
        last=market_price,
        underlying_price=_float(position.get("underlying_price")),
        meta={"reconciled": True, "source": "broker_position"},
    )
    side = "buy_to_open" if _int_qty(position.get("qty")) > 0 else "sell_to_open"
    max_loss = max(0.0, avg_entry * 100.0 * qty)
    return PositionSnapshot(
        strategy_id=f"reconciled-{symbol}",
        strategy_name="strategy_unknown",
        underlying=contract.underlying,
        legs=[OrderLeg(contract=contract, side=side, qty=qty)],
        opened_at=opened_at,
        entry_credit=round((-1.0 if side == "buy_to_open" else 1.0) * avg_entry * 100.0 * qty, 2),
        max_loss=max_loss,
        profit_take_pct=0.0,
        loss_stop_multiple=1.0,
        roll_threshold_delta=None,
    )


def _write_reconciliation_report(reports_dir: Path, payload: dict[str, Any], *, as_of: datetime) -> Path:
    directory = reports_dir / "reconciliation"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"reconciliation_{as_of:%Y%m%d-%H%M%S}.json"
    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True, default=str), lock=False)
    return path


def _int_qty(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
