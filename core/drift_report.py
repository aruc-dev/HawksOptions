"""Paper-trading versus backtest drift summaries."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean
from typing import Any

from core.backtest_engine import BacktestResult
from core.file_lock import atomic_write_text
from core.trade_log import read_trade_rows


def build_drift_summary(
    *,
    trade_log_path: Path,
    backtest_result: BacktestResult,
) -> dict[str, Any]:
    paper = _paper_summary(read_trade_rows(trade_log_path))
    backtest = _backtest_summary(backtest_result)
    return {
        "paper": paper,
        "backtest": backtest,
        "drift": {
            "closed_trade_count_delta": paper["closed_trade_count"] - backtest["closed_trade_count"],
            "avg_entry_price_drift": paper["avg_entry_price_drift"],
            "avg_hold_days_delta": _round_or_none(paper["avg_hold_days"], backtest["avg_hold_days"]),
            "total_slippage_delta": round(paper["total_leg_slippage_dollars"] - backtest["total_entry_slippage"], 2),
            "avg_slippage_delta": _round_or_none(
                paper["avg_leg_slippage_dollars"],
                backtest["avg_entry_slippage"],
            ),
            "pnl_dollars_delta": None,
            "pnl_pct_delta": _round_or_none(paper["avg_pnl_pct"], backtest["total_return_pct"]),
        },
        "limitations": [
            "Paper PnL is estimated from trade-log pnl_pct rows when present; the log does not always contain realized dollar PnL.",
            "Backtest exits are deterministic model exits, not broker fills.",
            "Backtest slippage is configured model slippage; paper slippage is captured only when expected/actual fill fields are logged.",
        ],
    }


def write_drift_report(summary: dict[str, Any], *, reports_dir: Path, days: int) -> Path:
    drift_dir = reports_dir / "drift"
    drift_dir.mkdir(parents=True, exist_ok=True)
    path = drift_dir / f"paper_vs_backtest_{days}d.md"
    paper = summary["paper"]
    backtest = summary["backtest"]
    drift = summary["drift"]
    lines = [
        "# HawksOptions Paper vs Backtest Drift",
        "",
        "This report compares available paper-trading log fields against the deterministic backtest.",
        "It is a drift diagnostic, not a live-trading expectancy estimate.",
        "",
        "## Paper Trading",
        "",
        f"- Strategy count: {paper['strategy_count']}",
        f"- Closed trade count: {paper['closed_trade_count']}",
        f"- Average expected entry price: {paper['avg_expected_entry_price']}",
        f"- Average actual entry price: {paper['avg_actual_entry_price']}",
        f"- Average entry price drift: {paper['avg_entry_price_drift']}",
        f"- Total leg slippage dollars: {paper['total_leg_slippage_dollars']}",
        f"- Average leg slippage dollars: {paper['avg_leg_slippage_dollars']}",
        f"- Average order duration seconds: {paper['avg_order_duration_seconds']}",
        f"- Average hold days: {paper['avg_hold_days']}",
        f"- Average pnl pct: {paper['avg_pnl_pct']}",
        f"- Exit reasons: {paper['exit_reasons']}",
        "",
        "## Backtest",
        "",
        f"- Trade count: {backtest['trade_count']}",
        f"- Closed trade count: {backtest['closed_trade_count']}",
        f"- Total return pct: {backtest['total_return_pct']}",
        f"- PnL dollars: {backtest['pnl_dollars']}",
        f"- Average hold days: {backtest['avg_hold_days']}",
        f"- Total entry slippage: {backtest['total_entry_slippage']}",
        f"- Average entry slippage: {backtest['avg_entry_slippage']}",
        f"- Exit model: {backtest['exit_model']}",
        "",
        "## Drift",
        "",
        f"- Closed trade count delta: {drift['closed_trade_count_delta']}",
        f"- Average entry price drift: {drift['avg_entry_price_drift']}",
        f"- Average hold days delta: {drift['avg_hold_days_delta']}",
        f"- Total slippage delta: {drift['total_slippage_delta']}",
        f"- Average slippage delta: {drift['avg_slippage_delta']}",
        f"- PnL pct delta: {drift['pnl_pct_delta']}",
        "",
        "## Limitations",
        "",
    ]
    lines.extend(f"- {item}" for item in summary["limitations"])
    lines.extend(["", "```json", json.dumps(summary, indent=2, sort_keys=True, default=str), "```", ""])
    atomic_write_text(path, "\n".join(lines), lock=False)
    return path


def _paper_summary(rows: list[dict[str, str]]) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        strategy_id = str(row.get("strategy_id", "")).strip()
        if strategy_id:
            grouped[strategy_id].append(row)
    expected_prices = _floats(row.get("expected_entry_price") for row in rows)
    actual_prices = _floats(row.get("actual_entry_price") for row in rows)
    slippages = _floats(row.get("leg_slippage_dollars") for row in rows)
    durations = _floats(row.get("order_duration_seconds") for row in rows)
    closed_rows = [row for row in rows if str(row.get("status", "")).lower() == "closed"]
    pnl_pcts = _floats(row.get("pnl_pct") for row in closed_rows)
    exit_reasons = Counter(str(row.get("exit_reason", "") or "unknown") for row in closed_rows)
    holds = [_hold_days(items) for items in grouped.values()]
    holds = [value for value in holds if value is not None]
    return {
        "strategy_count": len(grouped),
        "row_count": len(rows),
        "closed_trade_count": len({row.get("strategy_id") for row in closed_rows if row.get("strategy_id")}),
        "avg_expected_entry_price": _round_mean(expected_prices),
        "avg_actual_entry_price": _round_mean(actual_prices),
        "avg_entry_price_drift": _round_or_none(_round_mean(actual_prices), _round_mean(expected_prices)),
        "total_leg_slippage_dollars": round(sum(slippages), 2),
        "avg_leg_slippage_dollars": _round_mean(slippages),
        "avg_order_duration_seconds": _round_mean(durations),
        "avg_hold_days": _round_mean(holds),
        "avg_pnl_pct": _round_mean(pnl_pcts),
        "exit_reasons": dict(sorted(exit_reasons.items())),
    }


def _backtest_summary(result: BacktestResult) -> dict[str, Any]:
    attribution_rows = list(result.attribution.get("by_strategy", {}).values())
    total_slippage = sum(float(row.get("total_entry_slippage", 0.0)) for row in attribution_rows)
    trade_count = sum(int(row.get("trade_count", 0)) for row in attribution_rows)
    avg_slippage = (total_slippage / trade_count) if trade_count else 0.0
    return {
        "trade_count": result.trade_count,
        "closed_trade_count": result.closed_trade_count,
        "total_return_pct": result.total_return_pct,
        "pnl_dollars": round(result.ending_equity - result.starting_fund, 2),
        "avg_hold_days": result.metrics.get("avg_hold_days", 0.0),
        "total_entry_slippage": round(total_slippage, 2),
        "avg_entry_slippage": round(avg_slippage, 2),
        "exit_model": "continuous_risk_checks plus forced final-day close",
        "rejected_reasons": result.rejected_reasons,
    }


def _floats(values) -> list[float]:
    out: list[float] = []
    for value in values:
        try:
            text = str(value).strip()
            if text:
                out.append(float(text))
        except (TypeError, ValueError):
            continue
    return out


def _round_mean(values: list[float]) -> float | None:
    return round(mean(values), 4) if values else None


def _round_or_none(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(float(left) - float(right), 4)


def _parse_timestamp(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _hold_days(rows: list[dict[str, str]]) -> float | None:
    opened = [_parse_timestamp(row.get("timestamp", "")) for row in rows if str(row.get("status", "")).lower() in {"open", "partially_filled"}]
    closed = [_parse_timestamp(row.get("timestamp", "")) for row in rows if str(row.get("status", "")).lower() == "closed"]
    opened = [item for item in opened if item is not None]
    closed = [item for item in closed if item is not None]
    if not opened or not closed:
        return None
    return round((max(closed) - min(opened)).total_seconds() / 86400.0, 4)
