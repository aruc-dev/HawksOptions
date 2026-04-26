"""Strategy candidate scoring and selection helpers."""

from __future__ import annotations

from typing import Any

from core.config import strategy_config
from core.models import StrategyContext, StrategyOrder


SHORT_PREMIUM_STRATEGIES = {
    "cash_secured_put",
    "covered_call",
    "vertical_spread",
    "iron_condor",
    "earnings_iron_condor",
}

LONG_PREMIUM_STRATEGIES = {"calendar_spread"}


def strategy_weight(config: dict[str, Any], strategy_name: str) -> float:
    return float(strategy_config(config, strategy_name).get("weight", 1.0))


def score_order(order: StrategyOrder, context: StrategyContext, config: dict[str, Any]) -> float:
    """Return a deterministic score for ranking accepted candidates.

    The score intentionally stays simple: config weight is the main control,
    then credit/risk quality and volatility-regime fit break ties. All hard
    safety decisions still belong to strategy constructors and pre-trade gates.
    """
    weight = strategy_weight(config, order.strategy_name)
    risk = max(float(order.max_loss), 0.01)
    reward_to_risk = max(float(order.max_profit), 0.0) / risk
    iv_component = max(0.0, min(1.0, float(order.iv_rank) / 100.0))
    if order.strategy_name in LONG_PREMIUM_STRATEGIES:
        iv_component = 1.0 - iv_component
    current_iv = max(float(context.current_iv or 0.0), 0.01)
    regime_edge = max(0.0, (current_iv - float(context.realized_vol_20d or 0.0)) / current_iv)
    score = weight * (1.0 + reward_to_risk + (0.25 * iv_component) + (0.25 * regime_edge))
    order.metadata["selection_score"] = round(score, 6)
    return score


def select_best_order(
    candidates: list[tuple[float, StrategyOrder]],
) -> StrategyOrder | None:
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]
