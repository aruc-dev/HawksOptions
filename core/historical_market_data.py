"""Fixture-backed market-data replay for deterministic historical backtests."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from typing import Any

from core.alpaca_options_client import AlpacaOptionsClient
from core.config import BASE_DIR
from core.models import OptionContract


class HistoricalFixtureClient:
    """Replay underlying snapshots and option chains from a JSON fixture."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = deepcopy(config)
        backtest_cfg = self.config.get("backtest", {})
        fixture_file = backtest_cfg.get("fixture_file", "tests/fixtures/backtest_market_data.json")
        path = BASE_DIR / str(fixture_file)
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.snapshots = payload.get("underlyings", {})
        self.chains = payload.get("chains", {})
        self.fallback = AlpacaOptionsClient(config, use_sample_data=True)
        self.fallback_to_sample = bool(backtest_cfg.get("fixture_fallback_to_sample", False))

    def get_underlying_snapshot(self, symbol: str, as_of: date | None = None) -> dict[str, Any]:
        as_of = as_of or date.today()
        by_date = self.snapshots.get(symbol, {})
        payload = by_date.get(as_of.isoformat())
        if payload is None:
            if self.fallback_to_sample:
                return self.fallback.get_underlying_snapshot(symbol, as_of=as_of)
            return {"symbol": symbol, "price": 0.0, "current_iv": 0.0, "iv_rank": 0.0, "realized_vol_20d": 0.0, "atr_pct": 0.0}
        out = deepcopy(payload)
        out["symbol"] = symbol
        return out

    def get_option_chain(self, symbol: str, as_of: date | None = None) -> list[OptionContract]:
        as_of = as_of or date.today()
        contracts = self.chains.get(symbol, {}).get(as_of.isoformat())
        if contracts is None:
            if self.fallback_to_sample:
                return self.fallback.get_option_chain(symbol, as_of=as_of)
            return []
        return [_contract_from_payload(symbol, item) for item in contracts if isinstance(item, dict)]


def _contract_from_payload(symbol: str, payload: dict[str, Any]) -> OptionContract:
    return OptionContract(
        contract_symbol=str(payload["contract_symbol"]),
        underlying=symbol,
        option_type=str(payload["option_type"]),
        strike=float(payload["strike"]),
        expiration=date.fromisoformat(str(payload["expiration"])),
        bid=float(payload["bid"]),
        ask=float(payload["ask"]),
        last=float(payload.get("last", 0.0)),
        open_interest=int(payload.get("open_interest", 0)),
        volume=int(payload.get("volume", 0)),
        implied_volatility=float(payload.get("implied_volatility", 0.0)),
        delta=payload.get("delta"),
        theta=payload.get("theta"),
        vega=payload.get("vega"),
        gamma=payload.get("gamma"),
        underlying_price=float(payload.get("underlying_price", 0.0)),
        meta=dict(payload.get("meta", {})),
    )


def backtest_market_data_client(config: dict[str, Any]):
    backtest_cfg = config.get("backtest", {}) if isinstance(config, dict) else {}
    data_source = str(backtest_cfg.get("data_source", "sample")).lower()
    if data_source in {"fixture", "historical_fixture"}:
        return HistoricalFixtureClient(config)
    return AlpacaOptionsClient(config, use_sample_data=True)
