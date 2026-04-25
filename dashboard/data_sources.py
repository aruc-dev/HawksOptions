"""Read-only data loaders for the dashboard."""

from __future__ import annotations

import csv
import json
import logging
import re
import subprocess
from collections import defaultdict, deque
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from core.order_executor import load_positions
from core.risk_manager import aggregate_portfolio_greeks
from core.trade_log import read_trade_rows
from dashboard.config import cfg

log = logging.getLogger("dashboard.data_sources")
LOG_ISSUE_RE = re.compile(r"\b(CRITICAL|ERROR|WARNING|WARN)\b")
HEALTH_SNAPSHOT_NAME_RE = re.compile(r"^health_\d{8}T\d{6}\.json$")


def read_trades(path: Path | None = None) -> list[dict[str, Any]]:
    return read_trade_rows(path or cfg().trade_log_path)


def read_positions_snapshot(path: Path | None = None) -> list[dict[str, Any]]:
    positions = load_positions(path or cfg().positions_path)
    return [position.as_dict() for position in positions]


def read_daily_baseline(path: Path | None = None) -> dict[str, Any] | None:
    path = path or cfg().daily_baseline_path
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        return None
    return payload


def read_recent_log_lines(log_file: Path, max_lines: int = 50) -> list[str]:
    if not log_file.exists():
        return []
    with open(log_file, "r", errors="replace", encoding="utf-8") as handle:
        lines = deque(handle, maxlen=max_lines)
    return [line.rstrip("\n") for line in lines]


def read_recent_log_issues(logs_dir: Path | None = None, max_lines_per_file: int = 100) -> list[dict[str, str]]:
    root = logs_dir or cfg().logs_dir
    if not root.exists():
        return []
    issues = []
    for path in sorted(root.glob("*.log"), key=lambda item: item.stat().st_mtime, reverse=True):
        if path.name.startswith("dashboard_access_"):
            continue
        for line in read_recent_log_lines(path, max_lines=max_lines_per_file):
            match = LOG_ISSUE_RE.search(line)
            if not match:
                continue
            issues.append({"file": path.name, "level": "WARNING" if match.group(1) == "WARN" else match.group(1), "line": line})
            if len(issues) >= 20:
                return issues
    return issues


def latest_health_snapshot_path(snapshot_dir: Path | None = None) -> Path | None:
    root = snapshot_dir or cfg().health_snapshot_dir
    if not root.exists():
        return None
    candidates = [path for path in root.iterdir() if path.is_file() and HEALTH_SNAPSHOT_NAME_RE.match(path.name)]
    return sorted(candidates, key=lambda item: item.name)[-1] if candidates else None


def read_latest_health_snapshot(snapshot_dir: Path | None = None) -> dict[str, Any]:
    path = latest_health_snapshot_path(snapshot_dir)
    if path is None:
        return {"ok": False, "path": None, "data": None, "error": f"No health snapshot JSON found in {snapshot_dir or cfg().health_snapshot_dir}"}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return {"ok": False, "path": str(path), "data": None, "error": f"Could not read health snapshot at {path}: {exc}"}
    return {"ok": True, "path": str(path), "data": payload, "error": None}


def run_check_systemd(timeout_sec: int = 10) -> dict[str, Any]:
    script = cfg().check_systemd_script
    if not script.exists():
        return {"ok": False, "error": f"systemd check script not found: {script}", "stdout": "", "stderr": ""}
    try:
        result = subprocess.run(
            ["bash", str(script)],
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"systemd check timed out after {timeout_sec}s", "stdout": "", "stderr": ""}
    return {"ok": result.returncode == 0, "error": "" if result.returncode == 0 else result.stderr.strip(), "stdout": result.stdout, "stderr": result.stderr}


def build_open_strategy_rows(position_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for position in position_rows:
        rows.append(
            {
                "strategy_id": position.get("strategy_id"),
                "underlying": position.get("underlying"),
                "strategy_name": position.get("strategy_name"),
                "days_to_expiration": position.get("days_to_expiration"),
                "entry_credit": position.get("entry_credit"),
                "current_pnl": position.get("current_pnl"),
                "current_close_cost": position.get("current_close_cost"),
                "short_delta": position.get("short_delta"),
                "next_earnings_date": position.get("next_earnings_date"),
            }
        )
    return rows


def build_portfolio_greeks(position_rows: list[dict[str, Any]]) -> dict[str, float]:
    _ = position_rows
    snapshots = load_positions(cfg().positions_path)
    return aggregate_portfolio_greeks(snapshots)


def read_underlyings() -> list[dict[str, Any]]:
    with open(cfg().underlyings_path, "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    return list(payload.get("underlyings", []))


def build_earnings_calendar(position_rows: list[dict[str, Any]], *, as_of: date | None = None) -> list[dict[str, Any]]:
    as_of = as_of or date.today()
    open_underlyings = {str(row.get("underlying")) for row in position_rows}
    items = []
    for underlying in read_underlyings():
        earnings_text = underlying.get("next_earnings_date")
        if not earnings_text:
            continue
        earnings_date = date.fromisoformat(str(earnings_text))
        if earnings_date < as_of or earnings_date > (as_of + timedelta(days=14)):
            continue
        items.append(
            {
                "symbol": underlying["symbol"],
                "earnings_date": earnings_date.isoformat(),
                "has_open_position": underlying["symbol"] in open_underlyings,
                "status": "position_open" if underlying["symbol"] in open_underlyings else "watchlist_only",
            }
        )
    return sorted(items, key=lambda item: item["earnings_date"])


def build_iv_rank_heatmap(iv_history_path: Path | None = None) -> list[dict[str, Any]]:
    path = iv_history_path or cfg().iv_history_path
    if not path.exists():
        return []
    series: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    with open(path, "r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            symbol = str(row.get("symbol", "")).upper()
            ts_text = str(row.get("timestamp", ""))
            if ts_text.endswith("Z"):
                ts_text = ts_text[:-1] + "+00:00"
            try:
                ts = datetime.fromisoformat(ts_text)
                iv = float(row.get("implied_volatility", 0.0))
            except (ValueError, TypeError):
                continue
            series[symbol].append((ts, iv))
    out = []
    for symbol, values in sorted(series.items()):
        values.sort(key=lambda item: item[0])
        latest_iv = values[-1][1]
        ivs = [item[1] for item in values]
        low = min(ivs)
        high = max(ivs)
        iv_rank = 100.0 if high == low else round(100.0 * (latest_iv - low) / (high - low), 2)
        out.append({"symbol": symbol, "current_iv": round(latest_iv, 4), "iv_low": round(low, 4), "iv_high": round(high, 4), "iv_rank": iv_rank})
    return out


def read_ai_activity() -> dict[str, Any]:
    return {"enabled": cfg().ai_enabled, "vetoes_today": 0, "daily_spend_usd": 0.0, "last_latency_ms": None}
