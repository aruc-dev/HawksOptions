"""Deterministic sample-data backtest engine."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from math import sqrt
from pathlib import Path
from statistics import mean, pstdev
from typing import Any

from core.alpaca_options_client import AlpacaOptionsClient
from core.config import load_underlyings
from core.models import PositionSnapshot, StrategyContext
from core.order_executor import position_from_order
from core.risk_manager import continuous_risk_checks, pre_trade_check


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
    client: AlpacaOptionsClient,
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
    return path


def run_backtest(
    *,
    config: dict[str, Any],
    strategies: list[Any],
    days: int,
    starting_fund: float,
) -> tuple[BacktestResult, Path]:
    client = AlpacaOptionsClient(config, use_sample_data=True)
    underlyings = load_underlyings(config)
    slippage = _slippage_settings(config)
    current_equity = float(starting_fund)
    closed_pnls: list[float] = []
    open_positions: list[PositionSnapshot] = []
    trade_count = 0
    equity_curve = [current_equity]
    start_date = date.today() - timedelta(days=max(days, 10))

    for offset in range(days):
        as_of = start_date + timedelta(days=offset)
        if as_of.weekday() >= 5:
            equity_curve.append(current_equity)
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
                closed_pnls.append(position.current_pnl)
                current_equity += position.current_pnl
            else:
                still_open.append(position)
        open_positions = still_open

        account = {
            "equity": current_equity,
            "portfolio_value": current_equity,
            "cash": current_equity,
            "buying_power": current_equity * 2.0,
            "options_level": config.get("account", {}).get("options_level", 3),
        }
        for underlying in underlyings:
            symbol = underlying["symbol"]
            if any(position.underlying == symbol for position in open_positions):
                continue
            snapshot = client.get_underlying_snapshot(symbol, as_of=as_of)
            chain = client.get_option_chain(symbol, as_of=as_of)
            context = StrategyContext(
                underlying=underlying,
                chain=chain,
                config=config,
                account=account,
                iv_rank=float(snapshot["iv_rank"]),
                as_of=as_of,
                underlying_price=float(snapshot["price"]),
                next_earnings_date=underlying.get("next_earnings_date"),
                ex_dividend_date=underlying.get("ex_dividend_date"),
                dividend_amount=float(underlying.get("dividend_amount", 0.0)),
                realized_vol_20d=float(snapshot["realized_vol_20d"]),
                atr_pct=float(snapshot["atr_pct"]),
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
                    continue
                position = position_from_order(
                    order,
                    opened_at=datetime.combine(as_of, time(10, 0), tzinfo=timezone.utc),
                )
                _apply_entry_slippage(position, slippage)
                open_positions.append(position)
                trade_count += 1
                break
        mtm = sum(position.current_pnl for position in open_positions)
        equity_curve.append(round(current_equity + mtm, 2))

    ending_equity = equity_curve[-1] if equity_curve else current_equity
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
    )
    report_path = _write_report(result, reports_dir=Path(config["reporting"]["reports_dir"]), days=days)
    return result, report_path
