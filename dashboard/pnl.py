"""Pure P&L helpers for the dashboard."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _float(value: Any, default: float | None = 0.0) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _row_event_timestamp(row: dict[str, Any]) -> datetime | None:
    if str(row.get("status", "")).lower() == "closed":
        return _parse_iso(str(row.get("close_timestamp", "") or row.get("timestamp", "")))
    return _parse_iso(str(row.get("timestamp", "")))


def _row_entry_timestamp(row: dict[str, Any]) -> datetime | None:
    return _parse_iso(str(row.get("timestamp", "")))


def realized_pnl_for_row(row: dict[str, Any]) -> float:
    entry = _float(row.get("entry_price"))
    exit_price = _float(row.get("exit_price"))
    qty = _float(row.get("qty"), 1.0)
    side = str(row.get("side", "sell_to_open")).lower()
    if exit_price <= 0 or entry <= 0:
        return 0.0
    if side.startswith("sell"):
        return round((entry - exit_price) * qty * 100.0, 2)
    return round((exit_price - entry) * qty * 100.0, 2)


def realized_pnl_today(rows: Iterable[dict[str, Any]], now_utc: datetime | None = None) -> dict[str, Any]:
    now_utc = now_utc or datetime.now(timezone.utc)
    today = now_utc.date()
    total = 0.0
    count = 0
    for row in rows:
        if str(row.get("status", "")).lower() != "closed":
            continue
        ts = _row_event_timestamp(row)
        if ts is None or ts.date() != today:
            continue
        total += realized_pnl_for_row(row)
        count += 1
    return {"date": today.isoformat(), "total_usd": round(total, 2), "trade_count": count}


def realized_pnl_window(rows: Iterable[dict[str, Any]], *, lookback_days: int = 30, now_utc: datetime | None = None) -> dict[str, Any]:
    now_utc = now_utc or datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=max(1, int(lookback_days)))
    total = 0.0
    wins = 0
    losses = 0
    count = 0
    for row in rows:
        if str(row.get("status", "")).lower() != "closed":
            continue
        ts = _row_event_timestamp(row)
        if ts is None or ts < cutoff:
            continue
        pnl = realized_pnl_for_row(row)
        total += pnl
        count += 1
        if pnl > 0:
            wins += 1
        elif pnl < 0:
            losses += 1
    return {
        "window_days": lookback_days,
        "total_usd": round(total, 2),
        "trade_count": count,
        "wins": wins,
        "losses": losses,
    }


def strategy_summary(rows: Iterable[dict[str, Any]], *, lookback_days: int = 30, now_utc: datetime | None = None) -> list[dict[str, Any]]:
    now_utc = now_utc or datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=max(1, int(lookback_days)))
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"strategy": "unknown", "count": 0, "wins": 0, "losses": 0, "total_usd": 0.0})
    for row in rows:
        if str(row.get("status", "")).lower() != "closed":
            continue
        ts = _row_event_timestamp(row)
        if ts is None or ts < cutoff:
            continue
        strategy = str(row.get("strategy", "unknown"))
        bucket = buckets[strategy]
        bucket["strategy"] = strategy
        bucket["count"] += 1
        pnl = realized_pnl_for_row(row)
        bucket["total_usd"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
    out = []
    for bucket in buckets.values():
        count = bucket["count"] or 1
        bucket["win_rate"] = round(bucket["wins"] / count, 4)
        bucket["total_usd"] = round(bucket["total_usd"], 2)
        out.append(bucket)
    return sorted(out, key=lambda item: item["strategy"])


def slippage_summary(rows: Iterable[dict[str, Any]], *, lookback_days: int = 30, now_utc: datetime | None = None) -> list[dict[str, Any]]:
    now_utc = now_utc or datetime.now(timezone.utc)
    cutoff = now_utc - timedelta(days=max(1, int(lookback_days)))
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"strategy": "unknown", "leg_count": 0, "total_slippage_dollars": 0.0})
    for row in rows:
        ts = _row_entry_timestamp(row)
        if ts is None or ts < cutoff:
            continue
        slippage = _float(row.get("leg_slippage_dollars"), default=None)
        if slippage is None:
            continue
        strategy = str(row.get("strategy", "unknown"))
        bucket = buckets[strategy]
        bucket["strategy"] = strategy
        bucket["leg_count"] += 1
        bucket["total_slippage_dollars"] += slippage
    out = []
    for bucket in buckets.values():
        count = bucket["leg_count"] or 1
        bucket["total_slippage_dollars"] = round(bucket["total_slippage_dollars"], 2)
        bucket["average_slippage_dollars"] = round(bucket["total_slippage_dollars"] / count, 4)
        out.append(bucket)
    return sorted(out, key=lambda item: item["strategy"])


def daily_loss_headroom(baseline: dict[str, Any] | None, current_portfolio_value: float, daily_loss_limit_pct: float) -> dict[str, Any]:
    out = {
        "baseline_value": 0.0,
        "current_value": round(float(current_portfolio_value or 0.0), 2),
        "delta_usd": 0.0,
        "delta_pct": 0.0,
        "limit_pct": float(daily_loss_limit_pct),
        "limit_usd": 0.0,
        "remaining_usd": 0.0,
        "status": "unknown",
    }
    if not baseline:
        return out
    base = _float(baseline.get("portfolio_value"))
    if base <= 0:
        return out
    out["baseline_value"] = round(base, 2)
    delta = out["current_value"] - base
    out["delta_usd"] = round(delta, 2)
    out["delta_pct"] = round(delta / base, 6)
    out["limit_usd"] = round(base * daily_loss_limit_pct, 2)
    loss = -delta if delta < 0 else 0.0
    out["remaining_usd"] = round(out["limit_usd"] - loss, 2)
    if loss >= out["limit_usd"]:
        out["status"] = "tripped"
    elif loss >= out["limit_usd"] * 0.8:
        out["status"] = "critical"
    elif loss >= out["limit_usd"] * 0.5:
        out["status"] = "warn"
    else:
        out["status"] = "ok"
    return out
