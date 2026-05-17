"""Broker and market-data adapter protocols."""

from __future__ import annotations

from datetime import date
from typing import Any, Protocol, runtime_checkable

from core.models import OptionContract


@runtime_checkable
class MarketDataClient(Protocol):
    def get_underlying_snapshot(self, symbol: str, as_of: date | None = None) -> dict[str, Any]:
        ...

    def get_option_chain(self, symbol: str, as_of: date | None = None) -> list[OptionContract]:
        ...

    def get_option_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        ...


@runtime_checkable
class AccountClient(Protocol):
    def get_account(self) -> dict[str, Any]:
        ...

    def get_positions(self) -> list[dict[str, Any]]:
        ...


@runtime_checkable
class OrderSubmissionClient(Protocol):
    def submit_order(self, payload: dict[str, Any]) -> dict[str, Any]:
        ...


@runtime_checkable
class OrderExecutionClient(OrderSubmissionClient, Protocol):
    def get_option_quotes(self, symbols: list[str]) -> dict[str, dict[str, Any]]:
        ...


@runtime_checkable
class TradingClient(MarketDataClient, AccountClient, OrderSubmissionClient, Protocol):
    pass
