"""Historical market-data replay for deterministic backtests."""

from __future__ import annotations

import csv
import json
from copy import deepcopy
from datetime import date
from pathlib import Path
from typing import Any

from core.alpaca_options_client import AlpacaOptionsClient
from core.config import BASE_DIR
from core.models import OptionContract


class HistoricalDataSource:
    """Load normalized historical replay payloads from a concrete source."""

    def load(self) -> dict[str, Any]:  # pragma: no cover - interface
        raise NotImplementedError


class JsonHistoricalDataSource(HistoricalDataSource):
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        with open(self.path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            raise ValueError(f"{self.path} must contain a top-level JSON object")
        return payload


class CsvHistoricalDataSource(HistoricalDataSource):
    """Load replay data from normalized rows with a ``record_type`` column."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        with open(self.path, "r", encoding="utf-8", newline="") as handle:
            return _payload_from_rows(csv.DictReader(handle), source=str(self.path), provider="csv_replay")


class ParquetHistoricalDataSource(HistoricalDataSource):
    """Load replay data from Parquet when an optional pandas engine is present."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> dict[str, Any]:
        try:
            import pandas as pd  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - depends on optional runtime
            raise RuntimeError("Parquet replay requires pandas plus pyarrow or fastparquet") from exc
        try:
            frame = pd.read_parquet(self.path)
        except ImportError as exc:  # pragma: no cover - depends on optional runtime
            raise RuntimeError("Parquet replay requires pandas plus pyarrow or fastparquet") from exc
        return _payload_from_rows(frame.to_dict(orient="records"), source=str(self.path), provider="parquet_replay")


class ProviderHistoricalDataSource(HistoricalDataSource):
    """Boundary for future provider-backed historical data implementations."""

    def __init__(self, provider: str) -> None:
        self.provider = provider

    def load(self) -> dict[str, Any]:
        raise NotImplementedError(
            f"historical provider {self.provider!r} is not implemented; use JSON, CSV, or Parquet replay data"
        )


class HistoricalReplayClient:
    """Replay underlying snapshots, chains, and event metadata from JSON data."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = deepcopy(config)
        backtest_cfg = self.config.get("backtest", {})
        source_file = (
            backtest_cfg.get("fixture_file")
            or backtest_cfg.get("historical_data_file")
            or "tests/fixtures/backtest_market_data.json"
        )
        path = _resolve_path(source_file)
        payload = _data_source_for(backtest_cfg, path).load()
        self.path = path
        self.metadata = _mapping_section(payload, "metadata")
        self.provider = str(self.metadata.get("provider", payload.get("provider", "json_replay")))
        self.snapshots = _mapping_section(payload, "underlyings")
        self.chains = _mapping_section(payload, "chains")
        self.earnings = _mapping_section(payload, "earnings")
        self.dividends = _mapping_section(payload, "dividends")
        self.corporate_actions = _mapping_section(payload, "corporate_actions")
        self.fallback = AlpacaOptionsClient(config, use_sample_data=True)
        self.fallback_to_sample = bool(backtest_cfg.get("fixture_fallback_to_sample", False))

    def get_underlying_snapshot(self, symbol: str, as_of: date | None = None) -> dict[str, Any]:
        as_of = as_of or date.today()
        by_date = self.snapshots.get(symbol, {})
        payload = by_date.get(as_of.isoformat())
        if payload is None:
            if self.fallback_to_sample:
                return self.fallback.get_underlying_snapshot(symbol, as_of=as_of)
            return {
                "symbol": symbol,
                "price": 0.0,
                "current_iv": 0.0,
                "iv_rank": 0.0,
                "iv_percentile": 0.0,
                "realized_vol_20d": 0.0,
                "atr_pct": 0.0,
            }
        out = deepcopy(payload)
        out["symbol"] = symbol
        out.setdefault("iv_percentile", out.get("iv_rank", 0.0))
        earnings_date = self.next_earnings_date(symbol, as_of)
        dividend = self.next_dividend(symbol, as_of)
        if earnings_date is not None:
            out["next_earnings_date"] = earnings_date.isoformat()
        if dividend:
            out["ex_dividend_date"] = dividend.get("ex_dividend_date")
            out["dividend_amount"] = dividend.get("amount", 0.0)
        return out

    def get_option_chain(self, symbol: str, as_of: date | None = None) -> list[OptionContract]:
        as_of = as_of or date.today()
        contracts = self.chains.get(symbol, {}).get(as_of.isoformat())
        if contracts is None:
            if self.fallback_to_sample:
                return self.fallback.get_option_chain(symbol, as_of=as_of)
            return []
        return [
            _contract_from_payload(symbol, item)
            for item in contracts
            if isinstance(item, dict) and _listed_on(item, as_of)
        ]

    def next_earnings_date(self, symbol: str, as_of: date) -> date | None:
        dates = [
            date.fromisoformat(str(item.get("date")))
            for item in self.earnings.get(symbol, [])
            if isinstance(item, dict) and item.get("date")
        ]
        future_dates = sorted(item for item in dates if item >= as_of)
        return future_dates[0] if future_dates else None

    def next_dividend(self, symbol: str, as_of: date) -> dict[str, Any] | None:
        candidates = []
        for item in self.dividends.get(symbol, []):
            if not isinstance(item, dict) or not item.get("ex_dividend_date"):
                continue
            ex_date = date.fromisoformat(str(item["ex_dividend_date"]))
            if ex_date >= as_of:
                candidates.append((ex_date, item))
        if not candidates:
            return None
        return deepcopy(sorted(candidates, key=lambda item: item[0])[0][1])

    def corporate_actions_for(self, symbol: str, as_of: date) -> list[dict[str, Any]]:
        actions = []
        for item in self.corporate_actions.get(symbol, []):
            if not isinstance(item, dict) or not item.get("date"):
                continue
            if date.fromisoformat(str(item["date"])) == as_of:
                actions.append(deepcopy(item))
        return actions

    def coverage_summary(self) -> dict[str, Any]:
        symbols = sorted(set(self.snapshots) | set(self.chains))
        chain_dates = {
            symbol: sorted(set(self.chains.get(symbol, {})))
            for symbol in symbols
        }
        snapshot_dates = {
            symbol: sorted(set(self.snapshots.get(symbol, {})))
            for symbol in symbols
        }
        contract_count = sum(
            len(contracts)
            for by_date in self.chains.values()
            for contracts in by_date.values()
            if isinstance(contracts, list)
        )
        return {
            "provider": self.provider,
            "source": str(self.path),
            "symbols": symbols,
            "snapshot_dates": snapshot_dates,
            "chain_dates": chain_dates,
            "contract_count": contract_count,
            "earnings_symbols": sorted(self.earnings),
            "dividend_symbols": sorted(self.dividends),
            "corporate_action_symbols": sorted(self.corporate_actions),
        }


HistoricalFixtureClient = HistoricalReplayClient


def _resolve_path(value: Any) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else BASE_DIR / path


def _data_source_for(backtest_cfg: dict[str, Any], path: Path) -> HistoricalDataSource:
    source_type = str(backtest_cfg.get("historical_data_format", "")).lower()
    if not source_type:
        source_type = path.suffix.lower().lstrip(".") or "json"
    if source_type in {"json", "fixture"}:
        return JsonHistoricalDataSource(path)
    if source_type == "csv":
        return CsvHistoricalDataSource(path)
    if source_type in {"parquet", "pq"}:
        return ParquetHistoricalDataSource(path)
    if source_type in {"provider", "api"}:
        return ProviderHistoricalDataSource(str(backtest_cfg.get("historical_provider", "unknown")))
    raise ValueError(f"unsupported historical replay format: {source_type}")


def _clean_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        str(key): value
        for key, value in row.items()
        if value is not None and str(value) != "" and value == value
    }


def _payload_from_rows(rows: Any, *, source: str, provider: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "metadata": {"provider": provider, "source": source},
        "underlyings": {},
        "chains": {},
        "earnings": {},
        "dividends": {},
        "corporate_actions": {},
    }
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        row = _clean_row(raw)
        record_type = str(row.pop("record_type", row.pop("type", ""))).lower()
        symbol = str(row.pop("symbol", ""))
        row_date = str(row.pop("date", ""))
        if not record_type or not symbol:
            continue
        if record_type in {"snapshot", "underlying"} and row_date:
            payload["underlyings"].setdefault(symbol, {})[row_date] = row
        elif record_type in {"contract", "option"} and row_date:
            payload["chains"].setdefault(symbol, {}).setdefault(row_date, []).append(row)
        elif record_type == "earnings":
            if row_date and "date" not in row:
                row["date"] = row_date
            payload["earnings"].setdefault(symbol, []).append(row)
        elif record_type == "dividend":
            if row_date and "ex_dividend_date" not in row:
                row["ex_dividend_date"] = row_date
            payload["dividends"].setdefault(symbol, []).append(row)
        elif record_type in {"corporate_action", "corporate_actions"}:
            if row_date and "date" not in row:
                row["date"] = row_date
            payload["corporate_actions"].setdefault(symbol, []).append(row)
    return payload


def _mapping_section(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"historical replay section {key!r} must be a mapping")
    return value


def _listed_on(payload: dict[str, Any], as_of: date) -> bool:
    listed = payload.get("listed_date")
    if listed and date.fromisoformat(str(listed)) > as_of:
        return False
    delisted = payload.get("delisted_date")
    if delisted and date.fromisoformat(str(delisted)) <= as_of:
        return False
    return True


def _optional_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _contract_from_payload(symbol: str, payload: dict[str, Any]) -> OptionContract:
    meta = dict(payload.get("meta", {}))
    for key in ("listed_date", "delisted_date", "quote_timestamp", "provider"):
        if key in payload:
            meta[key] = payload[key]
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
        delta=_optional_float(payload.get("delta")),
        theta=_optional_float(payload.get("theta")),
        vega=_optional_float(payload.get("vega")),
        gamma=_optional_float(payload.get("gamma")),
        underlying_price=float(payload.get("underlying_price", 0.0)),
        meta=meta,
    )


def backtest_market_data_client(config: dict[str, Any]):
    backtest_cfg = config.get("backtest", {}) if isinstance(config, dict) else {}
    data_source = str(backtest_cfg.get("data_source", "sample")).lower()
    if data_source in {"fixture", "historical_fixture", "historical_replay", "json_replay"}:
        return HistoricalReplayClient(config)
    return AlpacaOptionsClient(config, use_sample_data=True)
