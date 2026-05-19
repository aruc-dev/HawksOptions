"""Thin Alpaca wrapper with deterministic sample-data fallback."""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.config import load_config, load_underlyings
from core.greeks_calculator import black_scholes_greeks
from core.iv_rank_tracker import compute_iv_percentile, compute_iv_rank, load_iv_history
from core.models import OptionContract
from core.occ import format_occ_symbol, parse_occ_symbol

TradingClient = None
OptionHistoricalDataClient = None
OptionLatestQuoteRequest = None
OptionChainRequest = None
OptionBarsRequest = None
TimeFrame = None
StockHistoricalDataClient = None
StockLatestQuoteRequest = None
GetOptionContractsRequest = None
AssetStatus = None

_LOGGER = logging.getLogger(__name__)


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


def _resolve_option_data_classes():
    global OptionHistoricalDataClient, OptionLatestQuoteRequest, OptionChainRequest
    if (
        OptionHistoricalDataClient is not None
        and OptionLatestQuoteRequest is not None
        and OptionChainRequest is not None
    ):
        return OptionHistoricalDataClient, OptionLatestQuoteRequest, OptionChainRequest
    try:
        from alpaca.data.historical.option import OptionHistoricalDataClient as AlpacaOptionHistoricalDataClient  # type: ignore
        from alpaca.data.requests import OptionChainRequest as AlpacaOptionChainRequest  # type: ignore
        from alpaca.data.requests import OptionLatestQuoteRequest as AlpacaOptionLatestQuoteRequest  # type: ignore
    except ImportError:  # pragma: no cover - optional dependency
        return None, None, None
    if OptionHistoricalDataClient is None:
        OptionHistoricalDataClient = AlpacaOptionHistoricalDataClient
    if OptionLatestQuoteRequest is None:
        OptionLatestQuoteRequest = AlpacaOptionLatestQuoteRequest
    if OptionChainRequest is None:
        OptionChainRequest = AlpacaOptionChainRequest
    return OptionHistoricalDataClient, OptionLatestQuoteRequest, OptionChainRequest


def _resolve_option_contract_classes():
    global GetOptionContractsRequest, AssetStatus
    if GetOptionContractsRequest is not None and AssetStatus is not None:
        return GetOptionContractsRequest, AssetStatus
    try:
        from alpaca.trading.enums import AssetStatus as AlpacaAssetStatus  # type: ignore
        from alpaca.trading.requests import GetOptionContractsRequest as AlpacaGetOptionContractsRequest  # type: ignore
    except ImportError:  # pragma: no cover - optional dependency
        return None, None
    GetOptionContractsRequest = AlpacaGetOptionContractsRequest
    AssetStatus = AlpacaAssetStatus
    return GetOptionContractsRequest, AssetStatus


def _resolve_option_bar_classes():
    global OptionBarsRequest, TimeFrame
    if OptionBarsRequest is not None and TimeFrame is not None:
        return OptionBarsRequest, TimeFrame
    try:
        from alpaca.data.requests import OptionBarsRequest as AlpacaOptionBarsRequest  # type: ignore
        from alpaca.data.timeframe import TimeFrame as AlpacaTimeFrame  # type: ignore
    except ImportError:  # pragma: no cover - optional dependency
        return None, None
    OptionBarsRequest = AlpacaOptionBarsRequest
    TimeFrame = AlpacaTimeFrame
    return OptionBarsRequest, TimeFrame


def _resolve_stock_data_classes():
    global StockHistoricalDataClient, StockLatestQuoteRequest
    if StockHistoricalDataClient is not None and StockLatestQuoteRequest is not None:
        return StockHistoricalDataClient, StockLatestQuoteRequest
    try:
        from alpaca.data.historical.stock import StockHistoricalDataClient as AlpacaStockHistoricalDataClient  # type: ignore
        from alpaca.data.requests import StockLatestQuoteRequest as AlpacaStockLatestQuoteRequest  # type: ignore
    except ImportError:  # pragma: no cover - optional dependency
        return None, None
    if StockHistoricalDataClient is None:
        StockHistoricalDataClient = AlpacaStockHistoricalDataClient
    if StockLatestQuoteRequest is None:
        StockLatestQuoteRequest = AlpacaStockLatestQuoteRequest
    return StockHistoricalDataClient, StockLatestQuoteRequest


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


def _request_with_optional_feed(request_class: Any, *, feed: str | None = None, **kwargs: Any) -> Any:
    request_kwargs = {key: value for key, value in kwargs.items() if value is not None}
    if feed:
        request_kwargs["feed"] = feed
    try:
        return request_class(**request_kwargs)
    except TypeError:
        if "feed" not in request_kwargs:
            raise
        request_kwargs.pop("feed", None)
        return request_class(**request_kwargs)


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
        self._option_data_client = None
        self._stock_data_client = None
        self._underlyings = {item["symbol"]: item for item in load_underlyings(self.config)}
        self._live_contract_meta_cache: dict[tuple[str, str, int, int, float, float], dict[str, Any]] = {}
        self._live_option_chain_cache: dict[tuple[str, str, int, int, float, float], dict[str, Any]] = {}
        self._live_underlying_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _mode(self) -> str:
        return str(self.config.get("mode", "paper")).lower()

    def _option_data_feed(self) -> str | None:
        market_data = self.config.get("market_data", {})
        configured_feed = market_data.get("option_feed", market_data.get("option_data_feed"))
        if configured_feed is not None:
            feed = str(configured_feed).strip().lower()
            return feed or None
        return None

    def _option_daily_bar_end(self, *, as_of: date, start: datetime) -> datetime | None:
        today_utc = datetime.now(timezone.utc).date()
        if self._mode() == "paper" and as_of >= today_utc:
            return None
        if as_of >= today_utc:
            return datetime.now(timezone.utc)
        return start + timedelta(days=1)

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

    def _get_option_data_client(self) -> Any:
        data_client_class, _, _ = _resolve_option_data_classes()
        if self.use_sample_data or data_client_class is None:
            return None
        if self._option_data_client is None:
            key, secret = _credentials(self._mode())
            if not key or not secret:
                raise RuntimeError("live Alpaca credentials are missing")
            self._option_data_client = data_client_class(key, secret)
        return self._option_data_client

    def _get_stock_data_client(self) -> Any:
        data_client_class, _ = _resolve_stock_data_classes()
        if self.use_sample_data or data_client_class is None:
            return None
        if self._stock_data_client is None:
            key, secret = _credentials(self._mode())
            if not key or not secret:
                raise RuntimeError("live Alpaca credentials are missing")
            self._stock_data_client = data_client_class(key, secret)
        return self._stock_data_client

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
        if not self.use_sample_data:
            return self._get_live_underlying_snapshot(symbol, as_of=as_of)
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
        if not self.use_sample_data:
            return self._get_live_option_chain(symbol, as_of=as_of)
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

        The sample-data path derives quotes from the deterministic chain. The
        live path uses Alpaca's option market-data client when alpaca-py is
        installed and credentials are configured.
        """
        if not self.use_sample_data:
            return self._get_live_option_quotes(symbols)
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
            timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            for contract_symbol in contract_symbols:
                contract = chain.get(contract_symbol)
                if contract is None:
                    continue
                out[contract_symbol] = {
                    "bid": contract.bid,
                    "ask": contract.ask,
                    "source": "sample_chain",
                    "timestamp": timestamp,
                }
        return out

    def get_market_volatility_snapshot(self, as_of: date | None = None) -> dict[str, Any]:
        if not self.use_sample_data:
            return self._get_live_market_volatility_snapshot(as_of=as_of)
        as_of = as_of or date.today()
        return {"vix": _sample_vix(as_of), "source": "sample_data", "as_of": as_of.isoformat()}

    def _get_live_option_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        client = self._get_option_data_client()
        _, request_class, _ = _resolve_option_data_classes()
        if client is None or request_class is None or not symbols:
            return {}
        request = _request_with_optional_feed(
            request_class,
            feed=self._option_data_feed(),
            symbol_or_symbols=symbols,
        )
        raw_quotes = client.get_option_latest_quote(request)
        quotes = raw_quotes if isinstance(raw_quotes, dict) else getattr(raw_quotes, "data", {})
        out: dict[str, dict[str, Any]] = {}
        if not isinstance(quotes, dict):
            return out
        for symbol, quote in quotes.items():
            bid = _quote_value(quote, "bid_price", "bp", "bid")
            ask = _quote_value(quote, "ask_price", "ap", "ask")
            if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
                continue
            item = {
                "bid": bid,
                "ask": ask,
                "source": "alpaca_option_latest_quote",
            }
            timestamp = _quote_datetime(quote, "timestamp", "t")
            if timestamp is not None:
                item["timestamp"] = timestamp.isoformat(timespec="seconds")
            out[str(symbol)] = item
        return out

    def _get_live_underlying_snapshot(self, symbol: str, as_of: date | None = None) -> dict[str, Any]:
        as_of = as_of or date.today()
        cache_key = (symbol.upper(), as_of.isoformat())
        cached = self._live_underlying_cache.get(cache_key)
        if cached is not None:
            return deepcopy(cached)
        client = self._get_stock_data_client()
        _, request_class = _resolve_stock_data_classes()
        if client is None or request_class is None:
            raise RuntimeError("live Alpaca stock data client is unavailable")
        request = request_class(symbol_or_symbols=[symbol])
        raw_quotes = client.get_stock_latest_quote(request)
        quotes = raw_quotes if isinstance(raw_quotes, dict) else getattr(raw_quotes, "data", {})
        quote = quotes.get(symbol) if isinstance(quotes, dict) else None
        bid = _quote_value(quote, "bid_price", "bp", "bid")
        ask = _quote_value(quote, "ask_price", "ap", "ask")
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            raise RuntimeError(f"live Alpaca stock quote is unavailable for {symbol}")
        price = round((bid + ask) / 2.0, 4)
        meta = deepcopy(self._underlyings.get(symbol, {"symbol": symbol}))
        iv_snapshot = self._live_underlying_iv_snapshot(symbol, as_of=as_of, spot=price)
        static_current_iv = _optional_float(meta.get("current_iv"))
        static_iv_rank = _optional_float(meta.get("iv_rank"))
        current_iv = static_current_iv
        if current_iv is None:
            current_iv = float(iv_snapshot.get("current_iv", 0.0))
        iv_rank = static_iv_rank
        if iv_rank is None:
            iv_rank = _optional_float(iv_snapshot.get("iv_rank"))
        iv_percentile = _optional_float(meta.get("iv_percentile"))
        if iv_percentile is None:
            iv_percentile = _optional_float(iv_snapshot.get("iv_percentile"))
        snapshot = {
            **meta,
            "symbol": symbol,
            "price": price,
            "current_iv": current_iv,
            "iv_rank": iv_rank if iv_rank is not None else 0.0,
            "iv_percentile": iv_percentile if iv_percentile is not None else (iv_rank if iv_rank is not None else 0.0),
            "realized_vol_20d": float(meta.get("realized_vol_20d", 0.0)),
            "atr_pct": float(meta.get("atr_pct", 0.0)),
            "source": "alpaca_stock_latest_quote",
            "iv_source": "underlying_metadata" if static_current_iv is not None else iv_snapshot.get("iv_source", "unavailable"),
            "iv_rank_source": "underlying_metadata" if static_iv_rank is not None else iv_snapshot.get("iv_rank_source", "unavailable"),
        }
        self._live_underlying_cache[cache_key] = snapshot
        return deepcopy(snapshot)

    def _get_live_option_chain(self, symbol: str, as_of: date | None = None) -> list[OptionContract]:
        as_of = as_of or date.today()
        client = self._get_option_data_client()
        _, _, request_class = _resolve_option_data_classes()
        if client is None or request_class is None:
            raise RuntimeError("live Alpaca option data client is unavailable")
        underlying_snapshot = self.get_underlying_snapshot(symbol, as_of=as_of)
        spot = float(underlying_snapshot["price"])
        min_dte, max_dte = self._live_chain_dte_range()
        strike_gte, strike_lte = self._live_chain_strike_bounds(spot)
        snapshots = self._get_live_option_chain_snapshots(
            symbol=symbol,
            as_of=as_of,
            min_dte=min_dte,
            max_dte=max_dte,
            strike_gte=strike_gte,
            strike_lte=strike_lte,
        )
        if not snapshots:
            return []
        meta_by_symbol = self._get_live_option_contract_metadata(
            symbol=symbol,
            as_of=as_of,
            min_dte=min_dte,
            max_dte=max_dte,
            strike_gte=strike_gte,
            strike_lte=strike_lte,
        )
        daily_volume_by_symbol = self._get_live_option_daily_volumes(
            symbols=[str(contract_symbol) for contract_symbol in snapshots],
            as_of=as_of,
        )
        contracts: list[OptionContract] = []
        for contract_symbol, snapshot in snapshots.items():
            contract_symbol = str(contract_symbol)
            try:
                parsed = parse_occ_symbol(contract_symbol)
            except ValueError:
                continue
            if str(parsed["underlying"]).upper() != symbol.upper():
                continue
            quote = _object_value(snapshot, "latest_quote")
            trade = _object_value(snapshot, "latest_trade")
            greeks = _object_value(snapshot, "greeks")
            bid = _quote_value(quote, "bid_price", "bp", "bid") or 0.0
            ask = _quote_value(quote, "ask_price", "ap", "ask") or 0.0
            last = _quote_value(trade, "price", "p", "last") or ((bid + ask) / 2.0 if bid > 0 and ask > 0 else 0.0)
            contract_meta = meta_by_symbol.get(contract_symbol, {})
            timestamp = _quote_datetime(quote, "timestamp", "t")
            daily_volume = daily_volume_by_symbol.get(contract_symbol)
            if daily_volume is not None:
                volume, volume_source = daily_volume, "daily_bar"
            else:
                volume, volume_source = 0, "unavailable"
            contract = OptionContract(
                contract_symbol=contract_symbol,
                underlying=str(parsed["underlying"]),
                option_type=str(parsed["option_type"]),
                strike=float(parsed["strike"]),
                expiration=parsed["expiration"],  # type: ignore[arg-type]
                bid=round(float(bid), 4),
                ask=round(float(ask), 4),
                last=round(float(last), 4),
                open_interest=_parse_int(_object_value(contract_meta, "open_interest"), default=0),
                volume=volume,
                implied_volatility=float(_object_value(snapshot, "implied_volatility") or 0.0),
                delta=_optional_float(_object_value(greeks, "delta")),
                theta=_optional_float(_object_value(greeks, "theta")),
                vega=_optional_float(_object_value(greeks, "vega")),
                gamma=_optional_float(_object_value(greeks, "gamma")),
                underlying_price=spot,
                meta={
                    "source": "alpaca_option_chain",
                    "quote_timestamp": timestamp.isoformat(timespec="seconds") if timestamp else None,
                    "volume_source": volume_source,
                    "open_interest_date": _date_text(_object_value(contract_meta, "open_interest_date")),
                },
            )
            contracts.append(contract)
        return sorted(contracts, key=lambda item: (item.expiration, item.strike, item.option_type))

    def _live_chain_dte_range(self) -> tuple[int, int]:
        gates = self.config.get("gates", {})
        min_dte = int(gates.get("min_dte_entry", 7))
        max_dte = int(gates.get("max_dte_entry", 55))
        return max(0, min_dte), max(min_dte, max_dte)

    def _live_chain_strike_bounds(self, spot: float) -> tuple[float, float]:
        market_data = self.config.get("market_data", {})
        window_pct = float(market_data.get("option_chain_strike_window_pct", 0.20))
        window_pct = max(0.01, min(window_pct, 1.0))
        return round(max(0.01, spot * (1.0 - window_pct)), 2), round(spot * (1.0 + window_pct), 2)

    def _get_live_option_chain_snapshots(
        self,
        *,
        symbol: str,
        as_of: date,
        min_dte: int,
        max_dte: int,
        strike_gte: float,
        strike_lte: float,
    ) -> dict[str, Any]:
        cache_key = (symbol.upper(), as_of.isoformat(), min_dte, max_dte, strike_gte, strike_lte)
        cached = self._live_option_chain_cache.get(cache_key)
        if cached is not None:
            return cached
        client = self._get_option_data_client()
        _, _, request_class = _resolve_option_data_classes()
        if client is None or request_class is None:
            raise RuntimeError("live Alpaca option data client is unavailable")
        request = _request_with_optional_feed(
            request_class,
            feed=self._option_data_feed(),
            underlying_symbol=symbol,
            expiration_date_gte=as_of + timedelta(days=min_dte),
            expiration_date_lte=as_of + timedelta(days=max_dte),
            strike_price_gte=strike_gte,
            strike_price_lte=strike_lte,
        )
        raw_chain = client.get_option_chain(request)
        snapshots = raw_chain if isinstance(raw_chain, dict) else getattr(raw_chain, "data", {})
        if not isinstance(snapshots, dict):
            snapshots = {}
        self._live_option_chain_cache[cache_key] = snapshots
        return snapshots

    def _live_underlying_iv_snapshot(self, symbol: str, *, as_of: date, spot: float) -> dict[str, Any]:
        client = self._get_option_data_client()
        _, _, request_class = _resolve_option_data_classes()
        if client is None or request_class is None:
            return {"current_iv": 0.0, "iv_rank": 0.0, "iv_percentile": 0.0, "iv_source": "unavailable", "iv_rank_source": "unavailable"}
        min_dte, max_dte = self._live_chain_dte_range()
        strike_gte, strike_lte = self._live_chain_strike_bounds(spot)
        try:
            snapshots = self._get_live_option_chain_snapshots(
                symbol=symbol,
                as_of=as_of,
                min_dte=min_dte,
                max_dte=max_dte,
                strike_gte=strike_gte,
                strike_lte=strike_lte,
            )
        except Exception as exc:
            _LOGGER.warning("live option IV snapshot unavailable for %s: %s", symbol, exc)
            return {"current_iv": 0.0, "iv_rank": 0.0, "iv_percentile": 0.0, "iv_source": "alpaca_option_chain_error", "iv_rank_source": "unavailable"}
        if not snapshots:
            return {"current_iv": 0.0, "iv_rank": 0.0, "iv_percentile": 0.0, "iv_source": "unavailable", "iv_rank_source": "unavailable"}
        chain_ivs = [
            iv
            for iv in (_optional_float(_object_value(snapshot, "implied_volatility")) for snapshot in snapshots.values())
            if iv is not None and iv > 0
        ]
        if not chain_ivs:
            return {"current_iv": 0.0, "iv_rank": 0.0, "iv_percentile": 0.0, "iv_source": "unavailable", "iv_rank_source": "unavailable"}
        current_iv = round(_median(chain_ivs), 6)
        reporting = self.config.get("reporting", {})
        history_path = Path(str(reporting.get("iv_history_file", "data/iv_rank_history.csv")))
        lookback_days = int(self.config.get("market_data", {}).get("iv_history_lookback_days", 365))
        trailing_ivs = load_iv_history(history_path, symbol, lookback_days=lookback_days)
        if trailing_ivs:
            return {
                "current_iv": current_iv,
                "iv_rank": compute_iv_rank(current_iv, trailing_ivs),
                "iv_percentile": compute_iv_percentile(current_iv, trailing_ivs),
                "iv_source": "alpaca_option_chain",
                "iv_rank_source": "iv_history",
            }
        return {
            "current_iv": current_iv,
            "iv_rank": 50.0,
            "iv_percentile": 50.0,
            "iv_source": "alpaca_option_chain",
            "iv_rank_source": "neutral_no_history",
        }

    def _get_live_option_contract_metadata(
        self,
        *,
        symbol: str,
        as_of: date,
        min_dte: int,
        max_dte: int,
        strike_gte: float,
        strike_lte: float,
    ) -> dict[str, Any]:
        cache_key = (symbol.upper(), as_of.isoformat(), min_dte, max_dte, strike_gte, strike_lte)
        cached = self._live_contract_meta_cache.get(cache_key)
        if cached is not None:
            return cached
        request_class, asset_status_class = _resolve_option_contract_classes()
        if request_class is None or asset_status_class is None:
            return {}
        try:
            client = self._get_trading_client()
        except Exception as exc:
            _LOGGER.warning("option contract metadata unavailable for %s: %s", symbol, exc)
            self._live_contract_meta_cache[cache_key] = {}
            return {}
        if client is None:
            return {}
        out: dict[str, Any] = {}
        page_token = None
        while True:
            request = request_class(
                underlying_symbols=[symbol],
                status=asset_status_class.ACTIVE,
                expiration_date_gte=as_of + timedelta(days=min_dte),
                expiration_date_lte=as_of + timedelta(days=max_dte),
                strike_price_gte=str(strike_gte),
                strike_price_lte=str(strike_lte),
                limit=10000,
                page_token=page_token,
            )
            try:
                response = client.get_option_contracts(request)
            except Exception as exc:
                _LOGGER.warning("option contract metadata lookup failed for %s: %s", symbol, exc)
                break
            contracts = _object_value(response, "option_contracts") or []
            for contract in contracts:
                contract_symbol = str(_object_value(contract, "symbol") or "")
                if contract_symbol:
                    out[contract_symbol] = contract
            page_token = _object_value(response, "next_page_token")
            if not page_token:
                break
        self._live_contract_meta_cache[cache_key] = out
        return out

    def _get_live_option_daily_volumes(self, *, symbols: list[str], as_of: date) -> dict[str, int]:
        request_class, timeframe_class = _resolve_option_bar_classes()
        if request_class is None or timeframe_class is None:
            return {}
        client = self._get_option_data_client()
        if client is None:
            return {}
        unique_symbols = sorted({symbol for symbol in symbols if symbol})
        if not unique_symbols:
            return {}
        market_data = self.config.get("market_data", {})
        batch_size = max(1, int(market_data.get("option_bar_batch_size", 200)))
        start = datetime.combine(as_of, datetime.min.time(), tzinfo=timezone.utc)
        end = self._option_daily_bar_end(as_of=as_of, start=start)
        out: dict[str, int] = {}
        for batch in _chunks(unique_symbols, batch_size):
            request = _request_with_optional_feed(
                request_class,
                feed=self._option_data_feed(),
                symbol_or_symbols=batch,
                timeframe=timeframe_class.Day,
                start=start,
                end=end,
                limit=1,
            )
            try:
                response = client.get_option_bars(request)
            except Exception as exc:
                _LOGGER.warning("option daily volume lookup failed: %s", exc)
                continue
            bars_by_symbol = response if isinstance(response, dict) else getattr(response, "data", {})
            if not isinstance(bars_by_symbol, dict):
                continue
            for symbol, bars in bars_by_symbol.items():
                if not bars:
                    continue
                latest_bar = bars[-1]
                volume = _parse_int(_object_value(latest_bar, "volume"), default=0)
                if volume > 0:
                    out[str(symbol)] = volume
        return out

    def _get_live_market_volatility_snapshot(self, as_of: date | None = None) -> dict[str, Any]:
        as_of = as_of or date.today()
        market_data = self.config.get("market_data", {})
        symbol = str(market_data.get("vix_symbol", "VIXY")).strip() or "VIXY"
        vix_scale = str(market_data.get("vix_symbol_scale", "proxy")).strip() or "proxy"
        try:
            client = self._get_stock_data_client()
        except Exception as exc:
            return {
                "vix": None,
                "source": "alpaca_stock_latest_quote_error",
                "symbol": symbol,
                "vix_scale": vix_scale,
                "reason": str(exc),
                "as_of": as_of.isoformat(),
            }
        _, request_class = _resolve_stock_data_classes()
        if client is None or request_class is None:
            return {
                "vix": None,
                "source": "alpaca_stock_latest_quote_unavailable",
                "symbol": symbol,
                "vix_scale": vix_scale,
                "as_of": as_of.isoformat(),
            }
        request = request_class(symbol_or_symbols=[symbol])
        try:
            raw_quotes = client.get_stock_latest_quote(request)
        except Exception as exc:
            return {
                "vix": None,
                "source": "alpaca_stock_latest_quote_error",
                "symbol": symbol,
                "vix_scale": vix_scale,
                "reason": str(exc),
                "as_of": as_of.isoformat(),
            }
        quotes = raw_quotes if isinstance(raw_quotes, dict) else getattr(raw_quotes, "data", {})
        quote = quotes.get(symbol) if isinstance(quotes, dict) else None
        bid = _quote_value(quote, "bid_price", "bp", "bid")
        ask = _quote_value(quote, "ask_price", "ap", "ask")
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            return {
                "vix": None,
                "source": "alpaca_stock_latest_quote_invalid",
                "symbol": symbol,
                "vix_scale": vix_scale,
                "as_of": as_of.isoformat(),
            }
        return {
            "vix": round((bid + ask) / 2.0, 4),
            "source": "alpaca_stock_latest_quote",
            "symbol": symbol,
            "vix_scale": vix_scale,
            "as_of": as_of.isoformat(),
        }

    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.use_sample_data or _resolve_trading_client_class() is None:
            return {
                "id": f"dryrun-{uuid4().hex[:12]}",
                "status": "accepted",
                "submitted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "payload": payload,
            }
        raise NotImplementedError("live order submission is intentionally not enabled in tests")


def _object_value(obj: Any, *names: str) -> Any:
    if obj is None:
        return None
    for name in names:
        value = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
        if value is not None:
            return value
    return None


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_int(value: Any, *, default: int = 0) -> int:
    if value in (None, ""):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _date_text(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value)


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def _quote_value(quote: Any, *names: str) -> float | None:
    if quote is None:
        return None
    for name in names:
        value = quote.get(name) if isinstance(quote, dict) else getattr(quote, name, None)
        if value in (None, ""):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _quote_datetime(quote: Any, *names: str) -> datetime | None:
    if quote is None:
        return None
    for name in names:
        value = quote.get(name) if isinstance(quote, dict) else getattr(quote, name, None)
        if value in (None, ""):
            continue
        if isinstance(value, datetime):
            parsed = value
        else:
            text = str(value).strip()
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                parsed = datetime.fromisoformat(text)
            except ValueError:
                continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None
