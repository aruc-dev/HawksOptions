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


def latest_candidate_scan_path(reports_dir: Path | None = None) -> Path | None:
    root = (reports_dir or cfg().reports_dir) / "candidate_scans"
    if not root.exists():
        return None
    candidates = sorted(path for path in root.glob("scan_*.json") if path.is_file())
    return candidates[-1] if candidates else None


def read_latest_candidate_scan(reports_dir: Path | None = None) -> dict[str, Any]:
    path = latest_candidate_scan_path(reports_dir)
    if path is None:
        return {"ok": False, "path": None, "data": None}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return {"ok": False, "path": str(path), "data": None, "error": str(exc)}
    return {"ok": True, "path": str(path), "data": payload}


def read_latest_rejection_summary(reports_dir: Path | None = None) -> dict[str, Any]:
    path = latest_candidate_scan_path(reports_dir)
    if path is None:
        return {"ok": False, "path": None, "summary": {"total_rejected": 0, "by_reason": {}, "by_strategy": {}, "by_stage": {}}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return {
            "ok": False,
            "path": str(path),
            "summary": {"total_rejected": 0, "by_reason": {}, "by_strategy": {}, "by_stage": {}},
            "error": str(exc),
        }
    rejected = payload.get("rejected", []) if isinstance(payload, dict) else []
    by_reason: dict[str, int] = defaultdict(int)
    by_strategy: dict[str, int] = defaultdict(int)
    by_stage: dict[str, int] = defaultdict(int)
    for item in rejected:
        if not isinstance(item, dict):
            continue
        by_strategy[str(item.get("strategy", "unknown"))] += 1
        by_stage[str(item.get("stage", "risk"))] += 1
        for reason in item.get("reasons", []) or []:
            by_reason[str(reason)] += 1
    return {
        "ok": True,
        "path": str(path),
        "summary": {
            "total_rejected": len(rejected),
            "by_reason": dict(sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))),
            "by_strategy": dict(sorted(by_strategy.items(), key=lambda item: (-item[1], item[0]))),
            "by_stage": dict(sorted(by_stage.items(), key=lambda item: (-item[1], item[0]))),
        },
    }


def latest_report_path(pattern: str, reports_dir: Path | None = None) -> Path | None:
    root = reports_dir or cfg().reports_dir
    if not root.exists():
        return None
    candidates = sorted(path for path in root.glob(pattern) if path.is_file())
    return candidates[-1] if candidates else None


def read_json_fenced_report(pattern: str, reports_dir: Path | None = None) -> dict[str, Any]:
    path = latest_report_path(pattern, reports_dir)
    if path is None:
        return {"ok": False, "path": None, "data": None}
    text = path.read_text(encoding="utf-8")
    marker = "```json"
    start = text.find(marker)
    if start < 0:
        return {"ok": False, "path": str(path), "data": None, "error": "JSON fence not found"}
    start += len(marker)
    end = text.find("```", start)
    if end < 0:
        return {"ok": False, "path": str(path), "data": None, "error": "JSON fence not closed"}
    try:
        payload = json.loads(text[start:end].strip())
    except json.JSONDecodeError as exc:
        return {"ok": False, "path": str(path), "data": None, "error": str(exc)}
    return {"ok": True, "path": str(path), "data": payload}


def read_latest_strategy_attribution(reports_dir: Path | None = None) -> dict[str, Any]:
    return read_json_fenced_report("strategy_attribution_*d.md", reports_dir)


def read_latest_drift_report(reports_dir: Path | None = None) -> dict[str, Any]:
    return read_json_fenced_report("drift/paper_vs_backtest_*d.md", reports_dir)


def read_latest_research_trace(reports_dir: Path | None = None) -> dict[str, Any]:
    path = latest_report_path("research_traces/research_trace_*.json", reports_dir)
    if path is None:
        return {"ok": False, "path": None, "data": None}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return {"ok": False, "path": str(path), "data": None, "error": str(exc)}
    return {"ok": True, "path": str(path), "data": payload}


def read_latest_ai_disagreements(reports_dir: Path | None = None) -> dict[str, Any]:
    path = latest_report_path("ai_disagreements/ai_disagreements_*.json", reports_dir)
    if path is None:
        return {"ok": False, "path": None, "data": None}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        return {"ok": False, "path": str(path), "data": None, "error": str(exc)}
    return {"ok": True, "path": str(path), "data": payload}


def build_risk_budget(position_rows: list[dict[str, Any]], account: dict[str, Any]) -> dict[str, Any]:
    account_cfg = cfg().account_config
    equity = float(account.get("portfolio_value") or account.get("equity") or 0.0)
    open_risk = round(sum(float(row.get("max_loss") or 0.0) for row in position_rows), 2)
    portfolio_cap_pct = float(account_cfg.get("max_portfolio_risk_pct", 0.0))
    single_cap_pct = float(account_cfg.get("max_single_position_risk_pct", 0.0))
    portfolio_cap = round(equity * portfolio_cap_pct, 2) if equity > 0 else 0.0
    return {
        "equity": equity,
        "open_risk": open_risk,
        "portfolio_cap_pct": portfolio_cap_pct,
        "portfolio_cap": portfolio_cap,
        "portfolio_cap_remaining": round(max(0.0, portfolio_cap - open_risk), 2),
        "single_position_cap_pct": single_cap_pct,
        "single_position_cap": round(equity * single_cap_pct, 2) if equity > 0 else 0.0,
        "open_position_count": len(position_rows),
    }


def build_candidate_funnel(candidate_scan: dict[str, Any]) -> dict[str, Any]:
    payload = candidate_scan.get("data") if isinstance(candidate_scan, dict) else None
    if not isinstance(payload, dict):
        return {"ok": False, "path": candidate_scan.get("path") if isinstance(candidate_scan, dict) else None}
    ranked = payload.get("ranked_candidates", []) if isinstance(payload.get("ranked_candidates"), list) else []
    return {
        "ok": True,
        "path": candidate_scan.get("path"),
        "candidate_count": payload.get("candidate_count", 0),
        "accepted_count": payload.get("accepted_count", 0),
        "rejected_count": payload.get("rejected_count", 0),
        "research_candidate_count": len(payload.get("research_candidates", []) or []),
        "top_candidates": ranked[:5],
        "chosen_orders": payload.get("chosen_orders", []),
    }


def read_dashboard_analytics(position_rows: list[dict[str, Any]], account: dict[str, Any]) -> dict[str, Any]:
    candidate_scan = read_latest_candidate_scan()
    scan_payload = candidate_scan.get("data") if candidate_scan.get("ok") else {}
    return {
        "candidate_funnel": build_candidate_funnel(candidate_scan),
        "scan_health": {
            "ok": bool(candidate_scan.get("ok") and isinstance(scan_payload, dict) and scan_payload.get("scan_health")),
            "path": candidate_scan.get("path"),
            "data": scan_payload.get("scan_health") if isinstance(scan_payload, dict) else None,
        },
        "strategy_attribution": read_latest_strategy_attribution(),
        "drift": read_latest_drift_report(),
        "research_trace": read_latest_research_trace(),
        "ai_disagreements": read_latest_ai_disagreements(),
        "risk_budget": build_risk_budget(position_rows, account),
    }


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
