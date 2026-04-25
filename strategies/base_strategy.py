"""Base interface and shared helpers for options strategies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date
from typing import Any

from core.config import strategy_config
from core.models import StrategyContext
from core.options_chain import filter_contracts


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


class BaseStrategy(ABC):
    name = "base"

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self.params = strategy_config(config, self.name)

    def enabled(self) -> bool:
        return bool(self.params.get("enabled", False))

    def allowed_for_underlying(self, context: StrategyContext) -> bool:
        return self.name in list(context.underlying.get("strategies_allowed", []))

    def next_earnings_date(self, context: StrategyContext) -> date | None:
        return _as_date(context.next_earnings_date or context.underlying.get("next_earnings_date"))

    def ex_dividend_date(self, context: StrategyContext) -> date | None:
        return _as_date(context.ex_dividend_date or context.underlying.get("ex_dividend_date"))

    def in_earnings_blackout(self, context: StrategyContext) -> bool:
        earnings_date = self.next_earnings_date(context)
        if earnings_date is None:
            return False
        blackout_days = int(self.config.get("gates", {}).get("earnings_blackout_days_before", 5))
        return (earnings_date - context.as_of).days <= blackout_days

    def filtered_chain(self, context: StrategyContext, option_type: str) -> list:
        gates = self.config.get("gates", {})
        min_dte = int(self.params.get("target_dte_min", self.params.get("dte_min", gates.get("min_dte_entry", 7))))
        max_dte = int(self.params.get("target_dte_max", self.params.get("dte_max", gates.get("max_dte_entry", 55))))
        return filter_contracts(
            context.chain,
            min_open_interest=int(gates.get("min_open_interest", 100)),
            min_daily_volume=int(gates.get("min_daily_volume", 10)),
            max_bid_ask_spread_pct=float(gates.get("max_bid_ask_spread_pct", 0.10)),
            min_dte=min_dte,
            max_dte=max_dte,
            as_of=context.as_of,
            option_type=option_type,
        )

    def strategy_id(self, context: StrategyContext) -> str:
        return f"{self.name}-{context.underlying['symbol']}-{context.as_of:%Y%m%d}"

    @abstractmethod
    def generate_order(self, context: StrategyContext):  # pragma: no cover - interface only
        raise NotImplementedError
