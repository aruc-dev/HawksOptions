"""Strategy candidate scoring and selection helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from core.config import strategy_config
from core.models import StrategyContext, StrategyOrder
from core.strategy_types import LONG_PREMIUM_STRATEGIES


def strategy_weight(config: dict[str, Any], strategy_name: str) -> float:
    return float(strategy_config(config, strategy_name).get("weight", 1.0))


@dataclass(frozen=True)
class StrategyCandidate:
    order: StrategyOrder
    context: StrategyContext
    score: float
    structural_severity: str = "none"
    warnings: list[str] = field(default_factory=list)

    @property
    def underlying(self) -> str:
        return self.order.underlying

    @property
    def strategy_name(self) -> str:
        return self.order.strategy_name

    def summary(self) -> dict[str, Any]:
        return {
            "underlying": self.underlying,
            "strategy": self.strategy_name,
            "score": round(float(self.score), 6),
            "warnings": self.warnings,
            "net_opening_credit": self.order.net_opening_credit,
            "max_profit": self.order.max_profit,
            "max_loss": self.order.max_loss,
            "required_buying_power": self.order.required_buying_power,
            "iv_rank": self.order.iv_rank,
            "selection": dict(self.order.metadata.get("selection", {})),
        }


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
    order.metadata["selection"] = {
        "strategy_weight": round(weight, 6),
        "reward_to_risk": round(reward_to_risk, 6),
        "iv_component": round(iv_component, 6),
        "regime_edge": round(regime_edge, 6),
    }
    return score


def build_candidate(
    order: StrategyOrder,
    *,
    context: StrategyContext,
    config: dict[str, Any],
    structural_severity: str = "none",
    warnings: list[str] | None = None,
) -> StrategyCandidate:
    return StrategyCandidate(
        order=order,
        context=context,
        score=score_order(order, context, config),
        structural_severity=structural_severity,
        warnings=list(warnings or []),
    )


def rank_candidates(candidates: list[StrategyCandidate]) -> list[StrategyCandidate]:
    return sorted(
        candidates,
        key=lambda candidate: (
            candidate.score,
            -candidate.order.required_buying_power,
            candidate.order.net_opening_credit,
            candidate.underlying,
            candidate.strategy_name,
        ),
        reverse=True,
    )


def select_best_order(
    candidates: list[tuple[float, StrategyOrder]],
) -> StrategyOrder | None:
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]
