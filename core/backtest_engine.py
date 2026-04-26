"""Deterministic sample-data backtest engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from math import sqrt
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from core.config import load_underlyings
from core.historical_market_data import backtest_market_data_client
from core.models import PositionSnapshot, StrategyContext
from core.order_executor import position_from_order
from core.risk_manager import continuous_risk_checks, pre_trade_check
from strategies.selection import score_order, select_best_order


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


def _slippage_settings(config: dict[str, Any]) -> dict[str, float]:
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
    return {
        "per_leg_cents": float(raw.get("per_leg_cents", 0.0)),
        "spread_pct": float(raw.get("spread_pct", 0.0)),
        "commission_per_contract": float(raw.get("commission_per_contract", 0.0)),
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
    cost_per_share = slip["per_leg_cents"] + slip["spread_pct"] * spread / 2.0
    return round(cost_per_share * 100.0 * qty + slip["commission_per_contract"] * qty, 4)


def _apply_entry_slippage(position: PositionSnapshot, slippage: dict[str, float]) -> PositionSnapshot:
    # Slippage always worsens the opening fill: less credit for credit trades,
    # and a larger debit for debit trades.
    entry_slippage = sum(_leg_slippage_cost(leg, slippage) for leg in position.legs)
    position.entry_credit = round(position.entry_credit - entry_slippage, 2)
    return position


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
    slip = slippage or {"per_leg_cents": 0.0, "spread_pct": 0.0, "commission_per_contract": 0.0}
    exit_slippage = 0.0
    for leg in position.legs:
        contract = chain.get(leg.contract.contract_symbol, leg.contract)
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
        if result.rejected_reasons:
            handle.write(f"- Rejected reasons: {result.rejected_reasons}\n")
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
    slippage = _slippage_settings(config)
    cash_balance = float(starting_fund)
    closed_pnls: list[float] = []
    open_positions: list[PositionSnapshot] = []
    trade_count = 0
    rejected_reasons: dict[str, int] = {}
    stock_inventory: dict[str, dict[str, float]] = {}
    start_date = start_date or (date.today() - timedelta(days=max(days, 10)))
    equity_curve = [float(starting_fund)]

    for offset in range(days):
        as_of = start_date + timedelta(days=offset)
        if as_of.weekday() >= 5:
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

        account = {
            "equity": portfolio_equity,
            "portfolio_value": portfolio_equity,
            "cash": cash_balance,
            "buying_power": portfolio_equity * 2.0,
            "options_level": config.get("account", {}).get("options_level", 3),
        }
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
                next_earnings_date=underlying.get("next_earnings_date"),
                ex_dividend_date=underlying.get("ex_dividend_date"),
                dividend_amount=float(underlying.get("dividend_amount", 0.0)),
                realized_vol_20d=float(snapshot["realized_vol_20d"]),
                atr_pct=float(snapshot["atr_pct"]),
                long_shares=long_shares,
                cost_basis=cost_basis,
            )
            candidates: list[tuple[float, Any]] = []
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
                candidates.append((score_order(order, context, config), order))
            order = select_best_order(candidates)
            if order is not None:
                position = position_from_order(
                    order,
                    opened_at=datetime.combine(as_of, time(10, 0), tzinfo=timezone.utc),
                )
                _apply_entry_slippage(position, slippage)
                open_positions.append(position)
                trade_count += 1
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
    )
    report_path = _write_report(result, reports_dir=Path(config["reporting"]["reports_dir"]), days=days)
    return result, report_path
