"""Shared helpers for scheduler scripts."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from core.alpaca_options_client import AlpacaOptionsClient
from core.config import BASE_DIR, ensure_runtime_dirs, load_config, load_underlyings, reporting_path
from core.models import OrderLeg, PositionSnapshot, StrategyContext
from core.order_executor import load_positions


def runtime_paths(config: dict[str, Any]) -> dict[str, Path]:
    return {
        "trade_log": reporting_path(config, "trade_log_file"),
        "positions": reporting_path(config, "positions_file"),
        "greeks_dir": reporting_path(config, "greeks_snapshot_dir"),
        "iv_history": reporting_path(config, "iv_history_file"),
        "reports_dir": reporting_path(config, "reports_dir"),
        "logs_dir": reporting_path(config, "logs_dir"),
        "baseline": BASE_DIR / "data" / "daily_loss_baseline.json",
    }


def load_runtime(config: dict[str, Any] | None = None) -> tuple[dict[str, Any], AlpacaOptionsClient, dict[str, Path]]:
    config = config or load_config()
    ensure_runtime_dirs(config)
    client = AlpacaOptionsClient(config)
    return config, client, runtime_paths(config)


def current_positions(paths: dict[str, Path]) -> list[PositionSnapshot]:
    return load_positions(paths["positions"])


def _short_call_extrinsic(legs) -> float:
    """Return the smallest remaining extrinsic across short call legs.

    For ex-dividend assignment risk, what matters is whether *any*
    short call has dividend > extrinsic. Picking the minimum across the
    short call legs gives the conservative (closest to assignment)
    value. Returns 0.0 if there are no short call legs (the ex-div
    handler will short-circuit on ``short_leg_itm`` anyway).
    """
    extrinsic_values: list[float] = []
    for leg in legs:
        if leg.side != "sell_to_open":
            continue
        contract = leg.contract
        if contract.option_type != "call":
            continue
        intrinsic = max(0.0, float(contract.underlying_price) - float(contract.strike))
        mid = float(contract.mid_price())
        extrinsic = max(0.0, mid - intrinsic)
        extrinsic_values.append(extrinsic)
    if not extrinsic_values:
        return 0.0
    return min(extrinsic_values)


def refresh_positions(
    positions: list[PositionSnapshot],
    *,
    client: AlpacaOptionsClient,
    as_of: date | None = None,
) -> list[PositionSnapshot]:
    as_of = as_of or date.today()
    refreshed: list[PositionSnapshot] = []
    for position in positions:
        chain = {contract.contract_symbol: contract for contract in client.get_option_chain(position.underlying, as_of=as_of)}
        close_cost = 0.0
        short_leg_itm = False
        refreshed_legs = []
        for leg in position.legs:
            contract = chain.get(leg.contract.contract_symbol, leg.contract)
            refreshed_legs.append(OrderLeg(contract=contract, side=leg.side, qty=leg.qty))
            if leg.side == "sell_to_open":
                close_cost += contract.mid_price() * 100.0 * leg.qty
                short_leg_itm = short_leg_itm or contract.is_itm()
            else:
                close_cost -= contract.mid_price() * 100.0 * leg.qty
        position.legs = refreshed_legs
        position.current_close_cost = round(max(close_cost, 0.0), 2)
        position.current_pnl = round(position.entry_credit - position.current_close_cost, 2)
        position.short_leg_itm = short_leg_itm
        # Recompute remaining extrinsic on short calls so the ex-div
        # close logic (assignment_handler.should_close_short_call_for_ex_div)
        # operates on current data, not the stale 0.0 default.
        position.remaining_extrinsic_value = round(
            _short_call_extrinsic(refreshed_legs), 4
        )
        refreshed.append(position)
    return refreshed


def build_context(
    *,
    config: dict[str, Any],
    client: AlpacaOptionsClient,
    underlying: dict[str, Any],
    account: dict[str, Any],
    open_positions: list[PositionSnapshot],
    as_of: date,
) -> StrategyContext:
    symbol = underlying["symbol"]
    snapshot = client.get_underlying_snapshot(symbol, as_of=as_of)
    return StrategyContext(
        underlying=underlying,
        chain=client.get_option_chain(symbol, as_of=as_of),
        config=config,
        account=account,
        iv_rank=float(snapshot["iv_rank"]),
        as_of=as_of,
        underlying_price=float(snapshot["price"]),
        next_earnings_date=underlying.get("next_earnings_date"),
        ex_dividend_date=underlying.get("ex_dividend_date"),
        dividend_amount=float(underlying.get("dividend_amount", 0.0)),
        realized_vol_20d=float(snapshot.get("realized_vol_20d", 0.0)),
        atr_pct=float(snapshot.get("atr_pct", 0.0)),
        long_shares=0,
        cost_basis=0.0,
        open_positions=tuple(open_positions),
    )


def configured_underlyings(config: dict[str, Any]) -> list[dict[str, Any]]:
    return load_underlyings(config)
