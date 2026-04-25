"""Strategy registry."""

from __future__ import annotations

from typing import Any

from strategies.calendar_spread import CalendarSpreadStrategy
from strategies.cash_secured_put import CashSecuredPutStrategy
from strategies.covered_call import CoveredCallStrategy
from strategies.earnings_iron_condor import EarningsIronCondorStrategy
from strategies.iron_condor import IronCondorStrategy
from strategies.vertical_spread import VerticalSpreadStrategy


STRATEGY_CLASSES = [
    CashSecuredPutStrategy,
    CoveredCallStrategy,
    VerticalSpreadStrategy,
    IronCondorStrategy,
    CalendarSpreadStrategy,
    EarningsIronCondorStrategy,
]


def build_strategies(config: dict[str, Any]) -> list[Any]:
    return [cls(config) for cls in STRATEGY_CLASSES]


def build_enabled_strategies(config: dict[str, Any]) -> list[Any]:
    return [strategy for strategy in build_strategies(config) if strategy.enabled()]
