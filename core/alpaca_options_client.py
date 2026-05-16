"""Thin Alpaca wrapper with deterministic sample-data fallback."""

from __future__ import annotations

import math
import os
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from core.config import load_config, load_underlyings
from core.greeks_calculator import black_scholes_greeks
from core.iv_rank_tracker import compute_iv_percentile, compute_iv_rank
from core.models import OptionContract
from core.occ import format_occ_symbol, parse_occ_symbol

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


@dataclass(frozen=True)
class SampleUnderlyingState:
    price: float
    base_iv: float


SAMPLE_MARKET = {
    "SPY": SampleUnderlyingState(price=525.0, base_iv=0.19),
    "QQQ": SampleUnderlyingState(price=447.0, base_iv=0.23),
    "IWM": SampleUnderlyingState(price=207.0, base_iv=0.26),
    "AAPL": SampleUnderlyingState(price=198.0, base_iv=0.31),
    "MSFT": SampleUnderlyingState(price=428.0, base_iv=0.27),
}


def _price_step(spot: float) -> float:
    if spot >= 400:
        return 1.0
    if spot >= 200:
        return 1.0
    if spot >= 100:
        return 0.5
    return 0.5


def _sample_price(symbol: str, as_of: date) -> float:
    state = SAMPLE_MARKET.get(symbol, SampleUnderlyingState(price=100.0, base_iv=0.25))
    phase = sum(ord(ch) for ch in symbol) / 31.0
    cycle = math.sin(as_of.toordinal() / 8.0 + phase)
    drift = math.cos(as_of.toordinal() / 27.0 + phase) * 0.015
    return round(state.price * (1.0 + 0.035 * cycle + drift), 2)


def _sample_iv(symbol: str, as_of: date) -> float:
    state = SAMPLE_MARKET.get(symbol, SampleUnderlyingState(price=100.0, base_iv=0.25))
    phase = sum(ord(ch) for ch in symbol) / 17.0
    cycle = math.sin(as_of.toordinal() / 13.0 + phase)
    return round(max(0.12, state.base_iv * (1.0 + 0.22 * cycle)), 4)


def _sample_vix(as_of: date) -> float:
    cycle = math.sin(as_of.toordinal() / 21.0)
    return round(max(10.0, 20.0 + (7.0 * cycle)), 2)


def _credentials(mode: str) -> tuple[str, str]:
    prefix = "ALPACA_OPTIONS_PAPER" if mode == "paper" else "ALPACA_OPTIONS_LIVE"
    key = os.getenv(f"{prefix}_API_KEY", "").strip()
    secret = os.getenv(f"{prefix}_SECRET_KEY", "").strip()
    return key, secret


class AlpacaOptionsClient:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        use_sample_data: bool | None = None,
    ) -> None:
        self.config = deepcopy(config or load_config())
        if use_sample_data is None:
            use_sample_data = bool(self.config.get("market_data", {}).get("use_sample_data", True))
        self.use_sample_data = bool(use_sample_data)
        self._trading_client = None
        self._underlyings = {item["symbol"]: item for item in load_underlyings(self.config)}

    def _mode(self) -> str:
        return str(self.config.get("mode", "paper")).lower()

    def _get_trading_client(self) -> Any:
        trading_client_class = _resolve_trading_client_class()
        if self.use_sample_data or trading_client_class is None:
            return None
        if self._trading_client is None:
            key, secret = _credentials(self._mode())
            if not key or not secret:
                raise RuntimeError("live Alpaca credentials are missing")
            self._trading_client = trading_client_class(key, secret, paper=self._mode() == "paper")
        return self._trading_client

    def get_account(self) -> dict[str, Any]:
        if self.use_sample_data or _resolve_trading_client_class() is None:
            return {
                "equity": 100000.0,
                "portfolio_value": 100000.0,
                "cash": 60000.0,
                "buying_power": 200000.0,
                "options_level": self.config.get("account", {}).get("options_level", 3),
            }
        account = self._get_trading_client().get_account()
        return {
            "equity": float(account.equity),
            "portfolio_value": float(account.portfolio_value),
            "cash": float(account.cash),
            "buying_power": float(account.buying_power),
            "options_level": self.config.get("account", {}).get("options_level", 3),
        }

    def get_positions(self) -> list[dict[str, Any]]:
        if self.use_sample_data or _resolve_trading_client_class() is None:
            return []
        positions = []
        for position in self._get_trading_client().get_all_positions():
            positions.append(
                {
                    "symbol": position.symbol,
                    "qty": float(position.qty),
                    "avg_entry_price": float(position.avg_entry_price),
                    "current_price": float(position.current_price),
                    "market_value": float(position.market_value),
                    "unrealized_pl": float(position.unrealized_pl),
                }
            )
        return positions

    def get_open_orders(self) -> list[dict[str, Any]]:
        if self.use_sample_data or _resolve_trading_client_class() is None:
            return []
        return []

    def get_underlying_snapshot(self, symbol: str, as_of: date | None = None) -> dict[str, Any]:
        as_of = as_of or date.today()
        meta = deepcopy(self._underlyings.get(symbol, {"symbol": symbol}))
        current_iv = _sample_iv(symbol, as_of)
        trailing_ivs = [current_iv * 0.75, current_iv * 0.90, current_iv * 1.10, current_iv * 1.35]
        iv_rank = compute_iv_rank(current_iv, trailing_ivs)
        iv_percentile = compute_iv_percentile(current_iv, trailing_ivs)
        meta.update(
            {
                "symbol": symbol,
                "price": _sample_price(symbol, as_of),
                "current_iv": current_iv,
                "iv_rank": iv_rank,
                "iv_percentile": iv_percentile,
                "realized_vol_20d": float(meta.get("realized_vol_20d", max(0.1, current_iv * 0.8))),
                "atr_pct": float(meta.get("atr_pct", 0.02)),
            }
        )
        return meta

    def get_option_chain(self, symbol: str, as_of: date | None = None) -> list[OptionContract]:
        as_of = as_of or date.today()
        spot = _sample_price(symbol, as_of)
        iv = _sample_iv(symbol, as_of)
        risk_free_rate = float(self.config.get("market_data", {}).get("risk_free_rate", 0.04))
        dividend_yield = float(self.config.get("market_data", {}).get("default_dividend_yield", 0.0))
        step = _price_step(spot)
        chain: list[OptionContract] = []
        for dte in range(7, 61):
            expiration = as_of + timedelta(days=dte)
            years = dte / 365.0
            for offset in range(-20, 21):
                strike = round(step * round((spot + (offset * step)) / step), 2)
                if strike <= 0:
                    continue
                for option_type in ("call", "put"):
                    greeks = black_scholes_greeks(
                        option_type=option_type,
                        spot=spot,
                        strike=strike,
                        years_to_expiration=years,
                        risk_free_rate=risk_free_rate,
                        volatility=iv,
                        dividend_yield=dividend_yield,
                    )
                    theoretical = max(0.05, greeks.price)
                    spread = max(0.04, min(0.22, theoretical * 0.04))
                    bid = round(max(0.01, theoretical - spread / 2.0), 2)
                    ask = round(bid + spread, 2)
                    chain.append(
                        OptionContract(
                            contract_symbol=format_occ_symbol(symbol, expiration, option_type, strike),
                            underlying=symbol,
                            option_type=option_type,
                            strike=strike,
                            expiration=expiration,
                            bid=bid,
                            ask=ask,
                            last=round(theoretical, 2),
                            open_interest=max(140, 1200 - (abs(offset) * 30)),
                            volume=max(20, 180 - (abs(offset) * 5)),
                            implied_volatility=iv,
                            delta=round(greeks.delta, 4),
                            theta=round(greeks.theta, 4),
                            vega=round(greeks.vega, 4),
                            gamma=round(greeks.gamma, 6),
                            underlying_price=spot,
                            meta={"quote_timestamp": datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc).replace(hour=16).isoformat()},
                        )
                    )
        return chain

    def get_option_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        """Return latest bid/ask quotes keyed by OCC symbol.

        The sample-data path derives quotes from the deterministic chain. Live
        provider wiring must be implemented before live order submission can
        rely on this method for real NBBO.
        """
        if not self.use_sample_data:
            return {}
        out: dict[str, dict[str, Any]] = {}
        by_underlying: dict[str, list[str]] = {}
        for symbol in symbols:
            try:
                underlying = str(parse_occ_symbol(symbol)["underlying"])
            except ValueError:
                continue
            by_underlying.setdefault(underlying, []).append(symbol)
        today = date.today()
        for underlying, contract_symbols in by_underlying.items():
            chain = {
                contract.contract_symbol: contract
                for contract in self.get_option_chain(underlying, as_of=today)
            }
            for contract_symbol in contract_symbols:
                contract = chain.get(contract_symbol)
                if contract is None:
                    continue
                out[contract_symbol] = {
                    "bid": contract.bid,
                    "ask": contract.ask,
                    "source": "sample_chain",
                }
        return out

    def get_market_volatility_snapshot(self, as_of: date | None = None) -> dict[str, Any]:
        if not self.use_sample_data:
            return {}
        as_of = as_of or date.today()
        return {"vix": _sample_vix(as_of), "source": "sample_data", "as_of": as_of.isoformat()}

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.use_sample_data or _resolve_trading_client_class() is None:
            return {
                "id": f"dryrun-{uuid4().hex[:12]}",
                "status": "accepted",
                "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "payload": payload,
            }
        raise NotImplementedError("live order submission is intentionally not enabled in tests")
