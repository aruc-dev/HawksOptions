"""Deterministic sample-data backtest engine."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from math import sqrt
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from core.config import load_underlyings
from core.historical_market_data import backtest_market_data_client
from core.models import PositionSnapshot, StrategyContext, StrategyOrder
from core.order_executor import position_from_order
from core.risk_manager import continuous_risk_checks, pre_trade_check
from strategies.selection import build_candidate, rank_candidates


@dataclass(frozen=True)
class BacktestResult:
    starting_fund: float
    ending_equity: float
    total_return_pct: float
    sharpe: float
    max_drawdown_pct: float
    win_rate: float
    trade_count: int
    closed_trade_count: int
    rejected_reasons: dict[str, int] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    attribution: dict[str, Any] = field(default_factory=dict)


def _daily_returns(equity_curve: list[float]) -> list[float]:
    out = []
    for previous, current in zip(equity_curve, equity_curve[1:]):
        if previous <= 0:
            continue
        out.append((current - previous) / previous)
    return out


def _sharpe_ratio(equity_curve: list[float]) -> float:
    returns = _daily_returns(equity_curve)
    if len(returns) < 2:
        return 0.0
    sigma = pstdev(returns)
    if sigma == 0:
        return 0.0
    return round((mean(returns) / sigma) * sqrt(252), 4)


def _max_drawdown(equity_curve: list[float]) -> float:
    peak = 0.0
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        if peak > 0:
            max_dd = max(max_dd, (peak - value) / peak)
    return round(max_dd, 4)


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = int(index)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] + ((ordered[upper] - ordered[lower]) * weight)


def _risk_ratio(numerator: float, denominator: float) -> float:
    if denominator == 0:
        return 0.0
    return round(numerator / denominator, 4)


def _richer_metrics(
    *,
    equity_curve: list[float],
    closed_trades: list[dict[str, Any]],
    days: int,
    exposure_days: int,
    starting_fund: float,
) -> dict[str, Any]:
    returns = _daily_returns(equity_curve)
    downside = [value for value in returns if value < 0]
    gains = [value for value in returns if value > 0]
    losses = [value for value in returns if value < 0]
    pnls = [float(item["pnl"]) for item in closed_trades]
    winning_pnls = [pnl for pnl in pnls if pnl > 0]
    losing_pnls = [pnl for pnl in pnls if pnl < 0]
    downside_dev = pstdev(downside) if len(downside) > 1 else 0.0
    annualized_mean_return = (mean(returns) * 252.0) if returns else 0.0
    annualized_downside_dev = downside_dev * sqrt(252.0)
    max_drawdown = _max_drawdown(equity_curve)
    total_return = ((equity_curve[-1] / equity_curve[0]) - 1.0) if len(equity_curve) > 1 and equity_curve[0] > 0 else 0.0
    annualized_return = ((1.0 + total_return) ** (365.0 / max(days, 1))) - 1.0 if total_return > -1.0 else -1.0
    var_95 = _percentile(returns, 0.05)
    cvar_tail = [value for value in returns if value <= var_95]
    gross_profit = sum(winning_pnls)
    gross_loss = abs(sum(losing_pnls))
    metrics = {
        "sortino": _risk_ratio(annualized_mean_return, annualized_downside_dev),
        "calmar": _risk_ratio(annualized_return, max_drawdown),
        "var_95_pct": round(var_95 * 100.0, 4),
        "cvar_95_pct": round((mean(cvar_tail) if cvar_tail else 0.0) * 100.0, 4),
        "omega": _risk_ratio(sum(gains), abs(sum(losses))),
        "tail_ratio": _risk_ratio(abs(_percentile(returns, 0.95)), abs(_percentile(returns, 0.05))),
        "profit_factor": _risk_ratio(gross_profit, gross_loss),
        "expectancy": round(mean(pnls), 2) if pnls else 0.0,
        "average_win": round(mean(winning_pnls), 2) if winning_pnls else 0.0,
        "average_loss": round(mean(losing_pnls), 2) if losing_pnls else 0.0,
        "max_consecutive_losses": _max_consecutive_losses(pnls),
        "exposure_time_pct": round((exposure_days / max(days, 1)) * 100.0, 2),
        "avg_hold_days": round(mean(float(item["hold_days"]) for item in closed_trades), 2) if closed_trades else 0.0,
        "pnl_by_strategy": _sum_by(closed_trades, "strategy"),
        "pnl_by_symbol": _sum_by(closed_trades, "underlying"),
        "return_by_strategy_pct": _pct_by(closed_trades, "strategy", denominator=starting_fund),
        "trade_count_by_strategy": _count_by(closed_trades, "strategy"),
    }
    return metrics


def _attribution_report(
    *,
    closed_trades: list[dict[str, Any]],
    starting_fund: float,
) -> dict[str, Any]:
    return {
        "by_strategy": _attribution_by(closed_trades, "strategy", starting_fund=starting_fund),
        "by_symbol": _attribution_by(closed_trades, "underlying", starting_fund=starting_fund),
    }


def _attribution_by(
    trades: list[dict[str, Any]],
    key: str,
    *,
    starting_fund: float,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade.get(key, "unknown"))].append(trade)
    out: dict[str, dict[str, Any]] = {}
    for name, items in sorted(grouped.items()):
        pnls = [float(item.get("pnl", 0.0)) for item in items]
        wins = sum(1 for pnl in pnls if pnl > 0)
        losses = sum(1 for pnl in pnls if pnl < 0)
        max_losses = [float(item.get("max_loss", 0.0)) for item in items]
        slippages = [float(item.get("entry_slippage", 0.0)) for item in items]
        holds = [float(item.get("hold_days", 0.0)) for item in items]
        out[name] = {
            "trade_count": len(items),
            "total_pnl": round(sum(pnls), 2),
            "return_pct": round((sum(pnls) / starting_fund) * 100.0, 4) if starting_fund > 0 else 0.0,
            "realized_drawdown": round(_realized_drawdown(pnls), 2),
            "win_rate": round((wins / len(items)) * 100.0, 2) if items else 0.0,
            "wins": wins,
            "losses": losses,
            "average_hold_days": round(mean(holds), 2) if holds else 0.0,
            "total_entry_slippage": round(sum(slippages), 2),
            "average_entry_slippage": round(mean(slippages), 2) if slippages else 0.0,
            "average_risk_used": round(mean(max_losses), 2) if max_losses else 0.0,
            "max_risk_used": round(max(max_losses), 2) if max_losses else 0.0,
            "risk_usage_pct_of_fund": round((sum(max_losses) / starting_fund) * 100.0, 4) if starting_fund > 0 else 0.0,
        }
    return out


def _realized_drawdown(pnls: list[float]) -> float:
    peak = 0.0
    equity = 0.0
    drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return drawdown


def _max_consecutive_losses(pnls: list[float]) -> int:
    longest = 0
    current = 0
    for pnl in pnls:
        if pnl < 0:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _sum_by(trades: list[dict[str, Any]], key: str) -> dict[str, float]:
    totals: dict[str, float] = {}
    for trade in trades:
        name = str(trade.get(key, ""))
        totals[name] = round(totals.get(name, 0.0) + float(trade.get("pnl", 0.0)), 2)
    return dict(sorted(totals.items()))


def _count_by(trades: list[dict[str, Any]], key: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for trade in trades:
        name = str(trade.get(key, ""))
        totals[name] = totals.get(name, 0) + 1
    return dict(sorted(totals.items()))


def _pct_by(trades: list[dict[str, Any]], key: str, *, denominator: float) -> dict[str, float]:
    if denominator <= 0:
        return {}
    return {
        name: round((value / denominator) * 100.0, 4)
        for name, value in _sum_by(trades, key).items()
    }


def _slippage_settings(config: dict[str, Any]) -> dict[str, Any]:
    """Return slippage parameters for the backtest.

    ``per_leg_cents`` is a flat $/share cost applied against each leg
    on entry and exit (so a 4-leg condor pays it 4 times). ``spread_pct``
    is an additional fraction of (ask - bid) charged on top, modeling
    fills worse than mid by some fraction of the half-spread. Both
    default to zero for backwards compatibility; set them via
    ``config['backtest']['slippage']``.
    """
    bt = config.get("backtest", {}) if isinstance(config, dict) else {}
    raw = bt.get("slippage", {}) if isinstance(bt, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    fill_model = str(bt.get("fill_model", raw.get("fill_model", "mid"))).lower()
    return {
        "per_leg_cents": float(raw.get("per_leg_cents", 0.0)),
        "spread_pct": float(raw.get("spread_pct", 0.0)),
        "commission_per_contract": float(raw.get("commission_per_contract", 0.0)),
        "fill_model": fill_model,
        "failed_fill_probability": max(0.0, min(float(raw.get("failed_fill_probability", 0.0)), 1.0)),
    }


def _config_hash(config: dict[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _backtest_provenance(
    *,
    config: dict[str, Any],
    client,
    underlyings: list[dict[str, Any]],
    start_date: date,
    days: int,
    slippage: dict[str, Any],
) -> dict[str, Any]:
    backtest_cfg = config.get("backtest", {}) if isinstance(config, dict) else {}
    coverage = client.coverage_summary() if hasattr(client, "coverage_summary") else {}
    return {
        "data_source": str(backtest_cfg.get("data_source", "sample")),
        "data_format": str(backtest_cfg.get("historical_data_format", "")),
        "date_range": {
            "start": start_date.isoformat(),
            "end": (start_date + timedelta(days=max(days - 1, 0))).isoformat(),
            "days": days,
        },
        "symbols": [str(item.get("symbol", "")) for item in underlyings],
        "option_chain_coverage": coverage,
        "fill_model": slippage["fill_model"],
        "slippage_model": {
            "per_leg_cents": slippage["per_leg_cents"],
            "spread_pct": slippage["spread_pct"],
            "failed_fill_probability": slippage["failed_fill_probability"],
        },
        "commission_model": {
            "commission_per_contract": slippage["commission_per_contract"],
        },
        "config_hash": _config_hash(config),
    }


def _leg_slippage_cost(leg, slip: dict[str, float]) -> float:
    """Per-leg dollar cost charged at entry or exit.

    ``per_leg_cents`` is a $/share figure. Multiply by 100 (per
    contract) and the leg quantity. ``spread_pct`` charges that
    fraction of the bid-ask spread per share. Commissions are flat per
    contract.
    """
    contract = leg.contract
    qty = max(1, int(leg.qty))
    spread = max(0.0, float(contract.ask) - float(contract.bid))
    spread_pct = _effective_spread_pct(leg, slip)
    cost_per_share = slip["per_leg_cents"] + spread_pct * spread / 2.0
    return round(cost_per_share * 100.0 * qty + slip["commission_per_contract"] * qty, 4)


def _entry_slippage_cost(position: PositionSnapshot, slippage: dict[str, float]) -> float:
    return round(sum(_leg_slippage_cost(leg, slippage) for leg in position.legs), 4)


def _effective_spread_pct(leg, slip: dict[str, float]) -> float:
    model = str(slip.get("fill_model", "mid")).lower()
    configured = float(slip.get("spread_pct", 0.0))
    if model in {"half_spread", "bid_ask", "pessimistic"}:
        return max(configured, 1.0)
    if model == "liquidity_based":
        contract = leg.contract
        open_interest = max(int(contract.open_interest), 0)
        volume = max(int(contract.volume), 0)
        liquidity_penalty = 0.0
        if open_interest < 100:
            liquidity_penalty += 0.5
        if volume < 10:
            liquidity_penalty += 0.5
        return min(2.0, max(configured, 1.0) + liquidity_penalty)
    return configured


def _fill_succeeds(order: StrategyOrder, slip: dict[str, float]) -> bool:
    probability = float(slip.get("failed_fill_probability", 0.0))
    if probability <= 0:
        return True
    if probability >= 1:
        return False
    digest = hashlib.sha256(order.strategy_id.encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket >= probability


def _apply_entry_slippage(position: PositionSnapshot, slippage: dict[str, float]) -> PositionSnapshot:
    # Slippage always worsens the opening fill: less credit for credit trades,
    # and a larger debit for debit trades.
    entry_slippage = _entry_slippage_cost(position, slippage)
    position.entry_credit = round(position.entry_credit - entry_slippage, 2)
    return position


def _expired_contract_from_snapshot(contract, *, underlying_price: float, as_of: date):
    intrinsic = 0.0
    if contract.option_type == "call":
        intrinsic = max(0.0, underlying_price - float(contract.strike))
    elif contract.option_type == "put":
        intrinsic = max(0.0, float(contract.strike) - underlying_price)
    state = "expired_itm" if intrinsic > 0 else "expired_otm"
    return replace(
        contract,
        bid=round(intrinsic, 4),
        ask=round(intrinsic, 4),
        last=round(intrinsic, 4),
        underlying_price=underlying_price,
        meta={**dict(contract.meta), "lifecycle_state": state, "valued_as_of": as_of.isoformat()},
    )


def _lifecycle_contract(original_contract, chain: dict[str, Any], client, as_of: date):
    contract = chain.get(original_contract.contract_symbol)
    if contract is not None:
        return contract
    if original_contract.days_to_expiration(as_of) <= 0:
        snapshot = client.get_underlying_snapshot(original_contract.underlying, as_of=as_of)
        return _expired_contract_from_snapshot(
            original_contract,
            underlying_price=float(snapshot.get("price", original_contract.underlying_price)),
            as_of=as_of,
        )
    return replace(
        original_contract,
        meta={**dict(original_contract.meta), "lifecycle_state": "stale_quote_fallback", "valued_as_of": as_of.isoformat()},
    )


def _mark_to_market(
    position: PositionSnapshot,
    client,
    as_of: date,
    *,
    slippage: dict[str, float] | None = None,
) -> PositionSnapshot:
    chain = {contract.contract_symbol: contract for contract in client.get_option_chain(position.underlying, as_of=as_of)}
    close_cost = 0.0
    legs = []
    short_leg_itm = False
    short_call_extrinsics: list[float] = []
    slip = slippage or {
        "per_leg_cents": 0.0,
        "spread_pct": 0.0,
        "commission_per_contract": 0.0,
        "fill_model": "mid",
        "failed_fill_probability": 0.0,
    }
    exit_slippage = 0.0
    for leg in position.legs:
        contract = _lifecycle_contract(leg.contract, chain, client, as_of)
        if leg.side == "sell_to_open":
            close_cost += contract.mid_price() * 100.0 * leg.qty
            short_leg_itm = short_leg_itm or contract.is_itm()
            if contract.option_type == "call" and contract.is_itm():
                intrinsic = max(0.0, float(contract.underlying_price) - float(contract.strike))
                short_call_extrinsics.append(max(0.0, float(contract.mid_price()) - intrinsic))
        else:
            close_cost -= contract.mid_price() * 100.0 * leg.qty
        refreshed_leg = type(leg)(contract=contract, side=leg.side, qty=leg.qty)
        legs.append(refreshed_leg)
        exit_slippage += _leg_slippage_cost(refreshed_leg, slip)
    position.legs = legs  # intentionally mutate the live backtest snapshot
    position.current_close_cost = round(close_cost + exit_slippage, 2)
    position.current_pnl = round(position.entry_credit - position.current_close_cost, 2)
    position.short_leg_itm = short_leg_itm
    position.remaining_extrinsic_value = (
        round(min(short_call_extrinsics), 4) if short_call_extrinsics else 0.0
    )
    return position


def _inventory_for(symbol: str, stock_inventory: dict[str, dict[str, float]]) -> tuple[int, float]:
    item = stock_inventory.get(symbol, {})
    return int(item.get("shares", 0)), float(item.get("cost_basis", 0.0))


def _add_stock_inventory(
    stock_inventory: dict[str, dict[str, float]],
    symbol: str,
    *,
    shares: int,
    cost_basis: float,
) -> None:
    if shares <= 0:
        return
    current = stock_inventory.setdefault(symbol, {"shares": 0.0, "cost_basis": 0.0})
    old_shares = float(current.get("shares", 0.0))
    old_basis = float(current.get("cost_basis", 0.0))
    new_shares = old_shares + shares
    current["cost_basis"] = round(((old_basis * old_shares) + (cost_basis * shares)) / new_shares, 4)
    current["shares"] = new_shares


def _remove_stock_inventory(stock_inventory: dict[str, dict[str, float]], symbol: str, *, shares: int) -> None:
    current = stock_inventory.get(symbol)
    if not current or shares <= 0:
        return
    current["shares"] = max(0.0, float(current.get("shares", 0.0)) - shares)
    if current["shares"] <= 0:
        current["cost_basis"] = 0.0


def _stock_market_value(
    stock_inventory: dict[str, dict[str, float]],
    client,
    as_of: date,
) -> float:
    value = 0.0
    for symbol, item in stock_inventory.items():
        shares = float(item.get("shares", 0.0))
        if shares <= 0:
            continue
        snapshot = client.get_underlying_snapshot(symbol, as_of=as_of)
        price = float(snapshot.get("price", item.get("cost_basis", 0.0)))
        value += shares * price
    return round(value, 2)


def _portfolio_equity(
    *,
    cash_balance: float,
    stock_inventory: dict[str, dict[str, float]],
    open_positions: list[PositionSnapshot],
    client,
    as_of: date,
) -> float:
    option_mtm = sum(position.current_pnl for position in open_positions)
    return round(cash_balance + _stock_market_value(stock_inventory, client, as_of) + option_mtm, 2)


def _vix_scaling_enabled(config: dict[str, Any]) -> bool:
    gates = config.get("gates") if isinstance(config, dict) else {}
    if not isinstance(gates, dict):
        return False
    scaling = gates.get("vix_iv_rank_scaling", {})
    return isinstance(scaling, dict) and bool(scaling.get("enabled", False))


def _apply_expiration_assignment(
    position: PositionSnapshot,
    stock_inventory: dict[str, dict[str, float]],
    as_of: date,
) -> float:
    """Convert simple CSP/covered-call expirations into stock inventory.

    Returns the cash delta from the resulting stock transaction. Option P&L is
    handled by the caller; this function only accounts for buying/selling the
    shares created by assignment. Multi-leg defined-risk assignments are not
    simulated here; those continue to close through the normal mark-to-market.
    """
    position_dte = min((leg.contract.days_to_expiration(as_of) for leg in position.legs), default=0)
    if position_dte > 0:
        return 0.0
    if position.strategy_name == "cash_secured_put" and len(position.legs) == 1:
        leg = position.legs[0]
        contract = leg.contract
        if leg.side == "sell_to_open" and contract.option_type == "put" and contract.underlying_price < contract.strike:
            shares = 100 * leg.qty
            adjusted_basis = max(0.0, contract.strike - (position.entry_credit / (100.0 * leg.qty)))
            _add_stock_inventory(stock_inventory, position.underlying, shares=shares, cost_basis=adjusted_basis)
            return round(-(contract.underlying_price * shares), 2)
    if position.strategy_name == "covered_call" and len(position.legs) == 1:
        leg = position.legs[0]
        contract = leg.contract
        if leg.side == "sell_to_open" and contract.option_type == "call" and contract.underlying_price > contract.strike:
            available_shares, _ = _inventory_for(position.underlying, stock_inventory)
            shares = min(100 * leg.qty, available_shares)
            _remove_stock_inventory(stock_inventory, position.underlying, shares=shares)
            return round(contract.underlying_price * shares, 2)
    return 0.0


def _write_report(
    result: BacktestResult,
    *,
    reports_dir: Path,
    days: int,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"backtest_{days}d.md"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# HawksOptions Backtest\n\n")
        handle.write(f"- Starting fund: ${result.starting_fund:,.2f}\n")
        handle.write(f"- Ending equity: ${result.ending_equity:,.2f}\n")
        handle.write(f"- Total return: {result.total_return_pct:.2f}%\n")
        handle.write(f"- Sharpe: {result.sharpe:.2f}\n")
        handle.write(f"- Max drawdown: {result.max_drawdown_pct:.2f}%\n")
        handle.write(f"- Win rate: {result.win_rate:.2f}%\n")
        handle.write(f"- Closed trades: {result.closed_trade_count}\n")
        if result.metrics:
            handle.write("\n## Metrics\n\n")
            handle.write("```json\n")
            handle.write(json.dumps(result.metrics, indent=2, sort_keys=True, default=str))
            handle.write("\n```\n")
        if result.rejected_reasons:
            handle.write(f"- Rejected reasons: {result.rejected_reasons}\n")
        if result.provenance:
            handle.write("\n## Data Provenance\n\n")
            handle.write("```json\n")
            handle.write(json.dumps(result.provenance, indent=2, sort_keys=True, default=str))
            handle.write("\n```\n")
    return path


def _write_attribution_report(
    attribution: dict[str, Any],
    *,
    reports_dir: Path,
    days: int,
) -> Path:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"strategy_attribution_{days}d.md"
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("# HawksOptions Strategy Attribution\n\n")
        handle.write("Breakdown by strategy and symbol. PnL is modeled backtest PnL, not live expectancy.\n\n")
        handle.write("## By Strategy\n\n")
        for name, row in attribution.get("by_strategy", {}).items():
            handle.write(
                f"- {name}: PnL ${row['total_pnl']:,.2f}, win rate {row['win_rate']:.2f}%, "
                f"avg hold {row['average_hold_days']:.2f}d, slippage ${row['total_entry_slippage']:,.2f}, "
                f"avg risk ${row['average_risk_used']:,.2f}\n"
            )
        handle.write("\n## By Symbol\n\n")
        for name, row in attribution.get("by_symbol", {}).items():
            handle.write(
                f"- {name}: PnL ${row['total_pnl']:,.2f}, win rate {row['win_rate']:.2f}%, "
                f"avg hold {row['average_hold_days']:.2f}d, slippage ${row['total_entry_slippage']:,.2f}, "
                f"avg risk ${row['average_risk_used']:,.2f}\n"
            )
        handle.write("\n```json\n")
        handle.write(json.dumps(attribution, indent=2, sort_keys=True, default=str))
        handle.write("\n```\n")
    return path


def run_backtest(
    *,
    config: dict[str, Any],
    strategies: list[Any],
    days: int,
    starting_fund: float,
    start_date: date | None = None,
) -> tuple[BacktestResult, Path]:
    client = backtest_market_data_client(config)
    underlyings = load_underlyings(config)
    config = {
        **config,
        "_underlying_metadata": {str(item.get("symbol", "")): item for item in underlyings},
    }
    slippage = _slippage_settings(config)
    cash_balance = float(starting_fund)
    closed_pnls: list[float] = []
    closed_trades: list[dict[str, Any]] = []
    open_positions: list[PositionSnapshot] = []
    open_trade_context: dict[str, dict[str, Any]] = {}
    trade_count = 0
    exposure_days = 0
    rejected_reasons: dict[str, int] = {}
    stock_inventory: dict[str, dict[str, float]] = {}
    start_date = start_date or (date.today() - timedelta(days=max(days, 10)))
    provenance = _backtest_provenance(
        config=config,
        client=client,
        underlyings=underlyings,
        start_date=start_date,
        days=days,
        slippage=slippage,
    )
    equity_curve = [float(starting_fund)]

    for offset in range(days):
        as_of = start_date + timedelta(days=offset)
        if as_of.weekday() >= 5:
            if open_positions:
                exposure_days += 1
            equity_curve.append(
                _portfolio_equity(
                    cash_balance=cash_balance,
                    stock_inventory=stock_inventory,
                    open_positions=open_positions,
                    client=client,
                    as_of=as_of,
                )
            )
            continue

        for position in list(open_positions):
            _mark_to_market(position, client, as_of, slippage=slippage)

        risk_payload = continuous_risk_checks(
            open_positions,
            config=config,
            as_of=datetime.combine(as_of, time(16, 0), tzinfo=timezone.utc),
        )
        close_ids = {
            item["strategy_id"]
            for item in risk_payload["actions"]
            if item["action"]
            in {
                "take_profit",
                "stop_loss",
                "time_exit",
                "close_before_earnings",
                "close_for_ex_div",
                "close_for_calendar_assignment",
            }
        }
        if offset == days - 1:
            close_ids.update(position.strategy_id for position in open_positions)
        still_open: list[PositionSnapshot] = []
        for position in open_positions:
            if position.strategy_id in close_ids:
                assignment_cash_delta = _apply_expiration_assignment(position, stock_inventory, as_of)
                closed_pnls.append(position.current_pnl)
                closed_trades.append(
                    {
                        "strategy": position.strategy_name,
                        "underlying": position.underlying,
                        "pnl": round(position.current_pnl, 2),
                        "opened_at": position.opened_at.isoformat(timespec="seconds"),
                        "closed_at": as_of.isoformat(),
                        "hold_days": max(0, (as_of - position.opened_at.date()).days),
                        **open_trade_context.pop(position.strategy_id, {}),
                    }
                )
                cash_balance += position.current_pnl + assignment_cash_delta
            else:
                still_open.append(position)
        open_positions = still_open

        portfolio_equity = _portfolio_equity(
            cash_balance=cash_balance,
            stock_inventory=stock_inventory,
            open_positions=open_positions,
            client=client,
            as_of=as_of,
        )
        market_context = {}
        if _vix_scaling_enabled(config):
            market_context_getter = getattr(client, "get_market_volatility_snapshot", None)
            market_context = market_context_getter(as_of=as_of) if callable(market_context_getter) else {}

        account = {
            "equity": portfolio_equity,
            "portfolio_value": portfolio_equity,
            "cash": cash_balance,
            "buying_power": portfolio_equity * 2.0,
            "options_level": config.get("account", {}).get("options_level", 3),
            "market_context": market_context,
        }
        candidate_pool = []
        for underlying in underlyings:
            symbol = underlying["symbol"]
            if any(position.underlying == symbol for position in open_positions):
                continue
            snapshot = client.get_underlying_snapshot(symbol, as_of=as_of)
            chain = client.get_option_chain(symbol, as_of=as_of)
            long_shares, cost_basis = _inventory_for(symbol, stock_inventory)
            context = StrategyContext(
                underlying=underlying,
                chain=chain,
                config=config,
                account=account,
                iv_rank=float(snapshot["iv_rank"]),
                as_of=as_of,
                underlying_price=float(snapshot["price"]),
                current_iv=float(snapshot.get("current_iv", 0.0)),
                iv_percentile=float(snapshot.get("iv_percentile", snapshot.get("iv_rank", 0.0))),
                next_earnings_date=underlying.get("next_earnings_date"),
                ex_dividend_date=underlying.get("ex_dividend_date"),
                dividend_amount=float(underlying.get("dividend_amount", 0.0)),
                realized_vol_20d=float(snapshot["realized_vol_20d"]),
                atr_pct=float(snapshot["atr_pct"]),
                trend_20d=snapshot.get("trend_20d", underlying.get("trend_20d")),
                trend_50d=snapshot.get("trend_50d", underlying.get("trend_50d")),
                rsi_14=snapshot.get("rsi_14", underlying.get("rsi_14")),
                price_vs_sma_50=snapshot.get("price_vs_sma_50", underlying.get("price_vs_sma_50")),
                long_shares=long_shares,
                cost_basis=cost_basis,
            )
            for strategy in strategies:
                order = strategy.generate_order(context)
                if order is None:
                    continue
                decision = pre_trade_check(
                    order,
                    account=account,
                    config=config,
                    open_positions=open_positions,
                    as_of=as_of,
                )
                if not decision.accepted:
                    for reason in decision.reasons:
                        rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                    continue
                candidate_pool.append(build_candidate(order, context=context, config=config, warnings=decision.warnings))
        selected_underlyings = {position.underlying for position in open_positions}
        max_open = int(config.get("account", {}).get("max_open_strategies", 8))
        for candidate in rank_candidates(candidate_pool):
            if len(open_positions) >= max_open:
                break
            if candidate.underlying in selected_underlyings:
                continue
            decision = pre_trade_check(
                candidate.order,
                account=account,
                config=config,
                open_positions=open_positions,
                as_of=as_of,
            )
            if not decision.accepted:
                for reason in decision.reasons:
                    rejected_reasons[reason] = rejected_reasons.get(reason, 0) + 1
                continue
            if not _fill_succeeds(candidate.order, slippage):
                rejected_reasons["fill_failed"] = rejected_reasons.get("fill_failed", 0) + 1
                continue
            position = position_from_order(
                candidate.order,
                opened_at=datetime.combine(as_of, time(10, 0), tzinfo=timezone.utc),
            )
            entry_slippage = _entry_slippage_cost(position, slippage)
            _apply_entry_slippage(position, slippage)
            open_trade_context[position.strategy_id] = {
                "max_loss": round(float(position.max_loss), 2),
                "entry_credit": round(float(position.entry_credit), 2),
                "entry_slippage": round(entry_slippage, 2),
            }
            open_positions.append(position)
            selected_underlyings.add(candidate.underlying)
            trade_count += 1
        if open_positions:
            exposure_days += 1
        equity_curve.append(
            _portfolio_equity(
                cash_balance=cash_balance,
                stock_inventory=stock_inventory,
                open_positions=open_positions,
                client=client,
                as_of=as_of,
            )
        )

    ending_equity = equity_curve[-1] if equity_curve else float(starting_fund)
    wins = sum(1 for pnl in closed_pnls if pnl > 0)
    attribution = _attribution_report(closed_trades=closed_trades, starting_fund=starting_fund)
    result = BacktestResult(
        starting_fund=starting_fund,
        ending_equity=ending_equity,
        total_return_pct=round(((ending_equity / starting_fund) - 1.0) * 100.0, 2),
        sharpe=_sharpe_ratio(equity_curve),
        max_drawdown_pct=round(_max_drawdown(equity_curve) * 100.0, 2),
        win_rate=round((wins / len(closed_pnls)) * 100.0, 2) if closed_pnls else 0.0,
        trade_count=trade_count,
        closed_trade_count=len(closed_pnls),
        rejected_reasons=rejected_reasons,
        provenance=provenance,
        metrics=_richer_metrics(
            equity_curve=equity_curve,
            closed_trades=closed_trades,
            days=days,
            exposure_days=exposure_days,
            starting_fund=starting_fund,
        ),
        attribution=attribution,
    )
    attribution_path = _write_attribution_report(attribution, reports_dir=Path(config["reporting"]["reports_dir"]), days=days)
    result.provenance["attribution_report_path"] = str(attribution_path)
    report_path = _write_report(result, reports_dir=Path(config["reporting"]["reports_dir"]), days=days)
    return result, report_path
