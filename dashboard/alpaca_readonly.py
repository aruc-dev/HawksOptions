"""Read-only Alpaca wrapper for the dashboard."""

from __future__ import annotations

import logging
import os
from typing import Any

from dashboard.config import cfg

log = logging.getLogger("dashboard.alpaca_readonly")
_trading_client: Any | None = None
TradingClient = None


def _resolve_trading_client_class():
    global TradingClient
    if TradingClient is not None:
        return TradingClient
    try:
        from alpaca.trading.client import TradingClient as AlpacaTradingClient  # type: ignore
    except ImportError:  # pragma: no cover - optional dependency
        return None
    TradingClient = AlpacaTradingClient
    return TradingClient


def _mode_prefix() -> str:
    return "ALPACA_OPTIONS_DASHBOARD_PAPER" if cfg().mode == "paper" else "ALPACA_OPTIONS_DASHBOARD_LIVE"


def _get_dashboard_credentials() -> tuple[str, str, bool]:
    prefix = _mode_prefix()
    key = os.environ.get(f"{prefix}_API_KEY", "").strip()
    secret = os.environ.get(f"{prefix}_SECRET_KEY", "").strip()
    if not key or not secret:
        raise RuntimeError(f"Missing {prefix}_API_KEY or {prefix}_SECRET_KEY for dashboard mode={cfg().mode}")
    return key, secret, cfg().mode == "paper"


def _get_trading_client() -> Any:
    global _trading_client
    trading_client_class = _resolve_trading_client_class()
    if trading_client_class is None:
        raise RuntimeError("alpaca-py is not installed")
    if _trading_client is None:
        key, secret, paper = _get_dashboard_credentials()
        _trading_client = trading_client_class(key, secret, paper=paper)
    return _trading_client


def get_account() -> Any:
    return _get_trading_client().get_account()


def get_all_positions() -> list[Any]:
    return list(_get_trading_client().get_all_positions())


def get_portfolio_value() -> float:
    return float(get_account().portfolio_value)


def get_cash() -> float:
    return float(get_account().cash)


def get_buying_power() -> float:
    return float(get_account().buying_power)


def _account_value_as_float(account: Any, field_name: str, default: float = 0.0) -> float:
    try:
        if isinstance(account, dict):
            return float(account.get(field_name, default) or default)
        return float(getattr(account, field_name, default) or default)
    except (TypeError, ValueError):
        return default


def _position_to_dict(position: Any) -> dict[str, Any]:
    def _g(name: str, default: Any = None) -> Any:
        if isinstance(position, dict):
            return position.get(name, default)
        return getattr(position, name, default)

    return {
        "symbol": str(_g("symbol", "") or ""),
        "qty": float(_g("qty", 0.0) or 0.0),
        "avg_entry_price": float(_g("avg_entry_price", 0.0) or 0.0),
        "current_price": float(_g("current_price", 0.0) or 0.0),
        "market_value": float(_g("market_value", 0.0) or 0.0),
        "cost_basis": float(_g("cost_basis", 0.0) or 0.0),
        "unrealized_pl": float(_g("unrealized_pl", 0.0) or 0.0),
        "unrealized_plpc": float(_g("unrealized_plpc", 0.0) or 0.0),
        "asset_class": str(_g("asset_class", "") or ""),
        "side": str(_g("side", "") or ""),
    }


def get_positions_as_dicts() -> list[dict[str, Any]]:
    try:
        return [_position_to_dict(item) for item in get_all_positions()]
    except Exception as exc:
        log.warning("Could not fetch positions from Alpaca: %s", exc)
        return []


def get_account_summary(account: Any | None = None) -> dict[str, float]:
    try:
        account = account if account is not None else get_account()
        return {
            "portfolio_value": _account_value_as_float(account, "portfolio_value"),
            "cash": _account_value_as_float(account, "cash"),
            "buying_power": _account_value_as_float(account, "buying_power"),
        }
    except Exception as exc:
        log.warning("Could not fetch account summary: %s", exc)
        return {}


def alpaca_reachable(account: Any | None = None) -> bool:
    try:
        return (account if account is not None else get_account()) is not None
    except Exception:
        return False


ALLOWED_FUNCTIONS = frozenset(
    {
        "get_account",
        "get_all_positions",
        "get_portfolio_value",
        "get_cash",
        "get_buying_power",
        "get_positions_as_dicts",
        "get_account_summary",
        "alpaca_reachable",
    }
)

FORBIDDEN_FUNCTIONS = frozenset({"submit_order", "cancel_order", "close_position", "close_all_positions"})
