"""FastAPI application for the read-only HawksOptions dashboard."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard import __version__
from dashboard.alpaca_readonly import alpaca_reachable, get_account, get_account_summary
from dashboard.config import cfg
from dashboard.data_sources import (
    build_earnings_calendar,
    build_iv_rank_heatmap,
    build_open_strategy_rows,
    build_portfolio_greeks,
    read_ai_activity,
    read_dashboard_analytics,
    read_daily_baseline,
    read_latest_health_snapshot,
    read_latest_rejection_summary,
    read_positions_snapshot,
    read_recent_log_issues,
    read_trades,
)
from dashboard.pnl import daily_loss_headroom, realized_pnl_today, realized_pnl_window, strategy_summary
from dashboard.security import AccessLogMiddleware, assert_production_auth_safe, require_auth

log = logging.getLogger("dashboard.app")
HERE = Path(__file__).resolve().parent
TEMPLATES = Jinja2Templates(directory=str(HERE / "templates"))
STATIC_FILES = StaticFiles(directory=str(HERE / "static"))


def create_app() -> FastAPI:
    assert_production_auth_safe()
    app = FastAPI(title="HawksOptions Dashboard", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(AccessLogMiddleware)

    @app.get("/", include_in_schema=False)
    async def index(request: Request, _: str = Depends(require_auth)) -> Any:
        return TEMPLATES.TemplateResponse(request, "dashboard.html", {"version": __version__, "mode": cfg().mode})

    @app.get("/static/{path:path}", include_in_schema=False)
    async def static_assets(request: Request, path: str, _: str = Depends(require_auth)) -> Response:
        return await STATIC_FILES.get_response(path, request.scope)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        ok = alpaca_reachable()
        return JSONResponse({"status": "ok" if ok else "degraded"}, status_code=200 if ok else 503)

    @app.get("/api/state")
    async def api_state(_: str = Depends(require_auth)) -> dict[str, Any]:
        return _build_state_snapshot()

    @app.get("/api/health")
    async def api_health(_: str = Depends(require_auth)) -> dict[str, Any]:
        return _build_health()

    @app.get("/api/positions")
    async def api_positions(_: str = Depends(require_auth)) -> dict[str, Any]:
        positions = read_positions_snapshot()
        return {"positions": build_open_strategy_rows(positions)}

    @app.get("/api/pnl/today")
    async def api_pnl_today(_: str = Depends(require_auth)) -> dict[str, Any]:
        rows = read_trades()
        account = get_account_summary()
        return {
            "realized": realized_pnl_today(rows),
            "realized_30d": realized_pnl_window(rows, lookback_days=30),
            "headroom": daily_loss_headroom(read_daily_baseline(), account.get("portfolio_value", 0.0), cfg().daily_loss_limit_pct),
        }

    @app.get("/api/trades/recent")
    async def api_trades_recent(limit: int = 30, _: str = Depends(require_auth)) -> dict[str, Any]:
        rows = [row for row in read_trades() if str(row.get("status", "")).lower() == "closed"]
        rows.sort(key=lambda row: str(row.get("timestamp", "")), reverse=True)
        return {"trades": rows[: max(1, min(int(limit or 30), 200))]}

    @app.get("/api/strategies/summary")
    async def api_strategies(_: str = Depends(require_auth)) -> dict[str, Any]:
        return {"strategies": strategy_summary(read_trades(), lookback_days=30)}

    @app.get("/api/rejections/summary")
    async def api_rejections(_: str = Depends(require_auth)) -> dict[str, Any]:
        return read_latest_rejection_summary()

    @app.get("/api/analytics")
    async def api_analytics(_: str = Depends(require_auth)) -> dict[str, Any]:
        account = get_account_summary()
        positions = read_positions_snapshot()
        return read_dashboard_analytics(positions, account)

    return app


def _build_state_snapshot() -> dict[str, Any]:
    rows = read_trades()
    position_rows = read_positions_snapshot()
    health = _build_health()
    try:
        account_obj = get_account()
    except Exception:
        account_obj = None
        account = {}
        reachable = False
    else:
        account = get_account_summary(account_obj)
        reachable = alpaca_reachable(account_obj)
    return {
        "version": __version__,
        "mode": cfg().mode,
        "server_time_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "account": account,
        "positions": build_open_strategy_rows(position_rows),
        "open_strategies": build_open_strategy_rows(position_rows),
        "portfolio_greeks": build_portfolio_greeks(position_rows),
        "iv_rank_heatmap": build_iv_rank_heatmap(),
        "upcoming_earnings": build_earnings_calendar(position_rows),
        "realized_today": realized_pnl_today(rows),
        "realized_30d": realized_pnl_window(rows, lookback_days=30),
        "daily_loss_headroom": daily_loss_headroom(read_daily_baseline(), account.get("portfolio_value", 0.0), cfg().daily_loss_limit_pct),
        "strategies": strategy_summary(rows, lookback_days=30),
        "rejections": read_latest_rejection_summary(),
        "analytics": read_dashboard_analytics(position_rows, account),
        "recent_trades": [row for row in rows if str(row.get("status", "")).lower() == "closed"][:10],
        "ai_activity": read_ai_activity(),
        "health": health,
        "alpaca_reachable": reachable,
    }


def _build_health() -> dict[str, Any]:
    snapshot = read_latest_health_snapshot()
    log_issues = read_recent_log_issues()
    if not snapshot["ok"]:
        return {
            "status": "red",
            "systemd": {
                "error": "Health snapshot unavailable. Check hawksoptions-health-check.service.",
                "stdout_tail": [],
            },
            "log_issues": log_issues,
        }
    data = snapshot["data"] or {}
    return {
        "status": data.get("overall_status", "green"),
        "systemd": {
            "error": None,
            "stdout_tail": data.get("stdout_tail", []),
        },
        "log_issues": log_issues,
    }


app = create_app()
