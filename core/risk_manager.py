"""Pre-trade gates and continuous risk checks."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Any, Iterable

from core.assignment_handler import calendar_front_assignment_risk, should_close_short_call_for_ex_div
from core.file_lock import atomic_write_text, locked_open
from core.models import OptionContract, PositionSnapshot, StrategyOrder


SHORT_PREMIUM_STRATEGIES = {
    "cash_secured_put",
    "covered_call",
    "vertical_spread",
    "iron_condor",
    "earnings_iron_condor",
}

LONG_PREMIUM_STRATEGIES = {"calendar_spread"}


@dataclass(frozen=True)
class PreTradeDecision:
    accepted: bool
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _equity(account: dict[str, Any]) -> float:
    for key in ("equity", "portfolio_value"):
        try:
            value = float(account.get(key, 0.0))
        except (TypeError, ValueError):
            value = 0.0
        if value > 0:
            return value
    return 0.0


def _cash(account: dict[str, Any]) -> float:
    try:
        return float(account.get("cash", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _buying_power(account: dict[str, Any]) -> float:
    try:
        return float(account.get("buying_power", 0.0))
    except (TypeError, ValueError):
        return 0.0


def _min_dte(order: StrategyOrder, as_of: date | None = None) -> int:
    ref = as_of or date.today()
    return min((leg.contract.days_to_expiration(ref) for leg in order.legs), default=0)


def _passes_liquidity(contract: OptionContract, gates: dict[str, Any]) -> bool:
    if contract.open_interest < int(gates.get("min_open_interest", 100)):
        return False
    if contract.volume < int(gates.get("min_daily_volume", 10)):
        return False
    return contract.spread_pct() <= float(gates.get("max_bid_ask_spread_pct", 0.10))


def _days_until(target: date | None, as_of: date) -> int | None:
    if target is None:
        return None
    return (target - as_of).days


def _conflicts_with_open_position(order: StrategyOrder, open_positions: Iterable[PositionSnapshot]) -> bool:
    incoming_short_types = {leg.contract.option_type for leg in order.short_legs}
    incoming_strikes = {leg.contract.strike for leg in order.short_legs}
    for position in open_positions:
        if position.underlying != order.underlying:
            continue
        existing_short_types = {leg.contract.option_type for leg in position.legs if leg.side == "sell_to_open"}
        existing_strikes = {leg.contract.strike for leg in position.legs if leg.side == "sell_to_open"}
        if incoming_short_types & existing_short_types and incoming_strikes & existing_strikes:
            return True
    return False


def pre_trade_check(
    order: StrategyOrder,
    *,
    account: dict[str, Any],
    config: dict[str, Any],
    open_positions: Iterable[PositionSnapshot],
    as_of: date | None = None,
    ai_result: dict[str, Any] | None = None,
) -> PreTradeDecision:
    as_of = as_of or date.today()
    reasons: list[str] = []
    warnings: list[str] = []
    mode = str(config.get("mode", "")).lower()
    gates = config.get("gates", {})
    account_cfg = config.get("account", {})
    equity = _equity(account)

    if mode not in {"paper", "live"}:
        reasons.append("invalid_mode")
    if equity <= 0:
        reasons.append("equity_unavailable")

    if int(account_cfg.get("options_level", 0)) < int(order.required_options_level):
        reasons.append("options_level_too_low")

    if equity < float(account_cfg.get("pdt_threshold_usd", 25000)) and not order.swing_only:
        reasons.append("pdt_swing_only_violation")

    open_positions = list(open_positions)
    open_risk = sum(float(position.max_loss) for position in open_positions)
    if open_risk + order.max_loss > float(account_cfg.get("max_portfolio_risk_pct", 0.2)) * equity:
        reasons.append("portfolio_risk_cap_exceeded")

    if order.max_loss > float(account_cfg.get("max_single_position_risk_pct", 0.05)) * equity:
        reasons.append("single_position_risk_cap_exceeded")

    if len(open_positions) >= int(account_cfg.get("max_open_strategies", 8)):
        reasons.append("max_open_strategies_reached")

    reserve_cash = float(account_cfg.get("reserve_cash_pct", 0.15)) * equity
    if _cash(account) < order.required_buying_power + reserve_cash and _buying_power(account) < order.required_buying_power:
        reasons.append("insufficient_cash_or_buying_power")

    for leg in order.legs:
        if not _passes_liquidity(leg.contract, gates):
            reasons.append("liquidity_gate_failed")
            break

    min_dte = _min_dte(order, as_of=as_of)
    if min_dte < int(gates.get("min_dte_entry", 7)) or min_dte > int(gates.get("max_dte_entry", 55)):
        reasons.append("dte_gate_failed")

    days_to_earnings = _days_until(order.next_earnings_date, as_of)
    if days_to_earnings is not None and days_to_earnings <= int(gates.get("earnings_blackout_days_before", 5)):
        reasons.append("earnings_blackout")

    if order.strategy_name in SHORT_PREMIUM_STRATEGIES and order.iv_rank < float(gates.get("min_iv_rank_for_short_premium", 30)):
        reasons.append("iv_rank_too_low_for_short_premium")
    if order.strategy_name in LONG_PREMIUM_STRATEGIES and order.iv_rank > float(gates.get("max_iv_rank_for_long_premium", 40)):
        reasons.append("iv_rank_too_high_for_long_premium")

    if _conflicts_with_open_position(order, open_positions):
        reasons.append("conflicting_position_exists")

    if order.ai_veto_reason:
        reasons.append("ai_veto")
    elif ai_result and ai_result.get("veto"):
        reasons.append("ai_veto")
    elif ai_result and ai_result.get("warning"):
        warnings.append(str(ai_result["warning"]))

    return PreTradeDecision(accepted=not reasons, reasons=reasons, warnings=warnings)


def aggregate_portfolio_greeks(positions: Iterable[PositionSnapshot]) -> dict[str, float]:
    totals = {"delta": 0.0, "theta": 0.0, "vega": 0.0, "gamma": 0.0}
    for position in positions:
        for leg in position.legs:
            sign = -1.0 if leg.side == "sell_to_open" else 1.0
            qty_scale = 100.0 * leg.qty
            totals["delta"] += sign * qty_scale * float(leg.contract.delta or 0.0)
            totals["theta"] += sign * qty_scale * float(leg.contract.theta or 0.0)
            totals["vega"] += sign * qty_scale * float(leg.contract.vega or 0.0)
            totals["gamma"] += sign * qty_scale * float(leg.contract.gamma or 0.0)
    return {key: round(value, 4) for key, value in totals.items()}


def identify_elevated_positions(
    positions: Iterable[PositionSnapshot],
    *,
    config: dict[str, Any],
    as_of: datetime | None = None,
) -> list[str]:
    as_of = as_of or datetime.now(timezone.utc)
    cutoff_text = str(config.get("schedule", {}).get("expiration_exit_cutoff_time", "15:15"))
    cutoff_hour, cutoff_minute = [int(part) for part in cutoff_text.split(":", 1)]
    gates_cfg = config.get("gates", {})
    calendar_slippage = float(gates_cfg.get("calendar_assignment_slippage", 0.05))
    time_exit_dte = int(gates_cfg.get("time_exit_dte", 21))
    flagged: list[str] = []
    for position in positions:
        loss_alert = position.current_close_cost >= (position.entry_credit * max(position.loss_stop_multiple * 0.75, 0.0))
        earnings_days = _days_until(position.next_earnings_date, as_of.date())
        if (
            position.days_to_expiration <= time_exit_dte
            or position.short_leg_itm
            or (
                position.roll_threshold_delta is not None
                and abs(position.short_delta) >= abs(position.roll_threshold_delta)
            )
            or should_close_short_call_for_ex_div(position, as_of=as_of.date())
            or calendar_front_assignment_risk(position, slippage=calendar_slippage)
            or (earnings_days is not None and earnings_days <= int(config.get("gates", {}).get("close_positions_days_before_earnings", 2)))
            or loss_alert
            or (
                position.days_to_expiration <= 0
                and as_of.time() >= time(cutoff_hour, cutoff_minute)
            )
        ):
            flagged.append(position.strategy_id)
    return flagged


def continuous_risk_checks(
    positions: Iterable[PositionSnapshot],
    *,
    config: dict[str, Any],
    as_of: datetime | None = None,
) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc)
    gates = config.get("gates", {})
    calendar_slippage = float(gates.get("calendar_assignment_slippage", 0.05))
    time_exit_dte = int(gates.get("time_exit_dte", 21))
    actions: list[dict[str, Any]] = []
    positions = list(positions)
    elevated = set(identify_elevated_positions(positions, config=config, as_of=as_of))
    for position in positions:
        if position.current_pnl >= position.entry_credit * position.profit_take_pct:
            actions.append({"strategy_id": position.strategy_id, "action": "take_profit"})
        if position.current_close_cost >= position.entry_credit * position.loss_stop_multiple:
            actions.append({"strategy_id": position.strategy_id, "action": "stop_loss"})
        if position.roll_threshold_delta is not None and abs(position.short_delta) >= abs(position.roll_threshold_delta):
            actions.append({"strategy_id": position.strategy_id, "action": "roll_review"})
        if position.days_to_expiration <= time_exit_dte:
            actions.append({"strategy_id": position.strategy_id, "action": "time_exit"})
        if should_close_short_call_for_ex_div(position, as_of=as_of.date()):
            actions.append({"strategy_id": position.strategy_id, "action": "close_for_ex_div"})
        if calendar_front_assignment_risk(position, slippage=calendar_slippage):
            actions.append(
                {"strategy_id": position.strategy_id, "action": "close_for_calendar_assignment"}
            )
        earnings_days = _days_until(position.next_earnings_date, as_of.date())
        if earnings_days is not None and earnings_days <= int(gates.get("close_positions_days_before_earnings", 2)):
            actions.append({"strategy_id": position.strategy_id, "action": "close_before_earnings"})
    return {
        "generated_at": as_of.isoformat(timespec="seconds"),
        "portfolio_greeks": aggregate_portfolio_greeks(positions),
        "actions": actions,
        "elevated_positions": sorted(elevated),
    }


def daily_loss_status(
    baseline_value: float,
    current_value: float,
    *,
    halt_pct: float = 0.05,
    hard_close_pct: float = 0.08,
) -> dict[str, Any]:
    if baseline_value <= 0:
        return {"status": "unknown", "loss_pct": 0.0}
    loss_pct = max(0.0, (baseline_value - current_value) / baseline_value)
    if loss_pct >= hard_close_pct:
        status = "hard_close"
    elif loss_pct >= halt_pct:
        status = "halt_new_entries"
    else:
        status = "ok"
    return {"status": status, "loss_pct": round(loss_pct, 4)}


def read_daily_baseline(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    with locked_open(path, "r", lock="shared") as handle:
        return json.load(handle)


def write_daily_baseline(path: Path, portfolio_value: float, *, as_of: datetime | None = None) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc)
    payload = {
        "date": as_of.date().isoformat(),
        "portfolio_value": round(float(portfolio_value), 2),
        "created_at": as_of.isoformat(timespec="seconds"),
        "session_timezone": "America/New_York",
    }
    atomic_write_text(path, json.dumps(payload, indent=2))
    return payload


def write_greeks_snapshot(
    directory: Path,
    payload: dict[str, Any],
    *,
    as_of: datetime | None = None,
) -> Path:
    as_of = as_of or datetime.now(timezone.utc)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{as_of:%Y%m%d-%H%M%S}.json"
    atomic_write_text(path, json.dumps(payload, indent=2))
    return path
