"""Limit-price improvement rules for option order entry."""

from __future__ import annotations

from functools import reduce
from math import gcd
from typing import Any

from core.models import OrderLeg, StrategyOrder


def limit_price_improvement_plan(
    order: StrategyOrder,
    *,
    config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a bounded limit-price widening schedule for an opening order.

    The schedule starts at the package mid and widens only toward the
    package's unfavorable bid/ask edge. Credit orders never widen below
    configured minimum credit quality, and debit orders never widen beyond
    max-loss based debit limits.
    """
    unit_qty = _unit_qty(order)
    mid_credit = float(order.net_opening_credit)
    worst_credit = _worst_package_credit(order)
    settings = _settings(order, config or {})
    enabled = bool(settings.get("enabled", True))
    steps = max(1, int(settings.get("steps", 3) or 3))
    concession_pct = _liquidity_adjusted_concession(order, settings)
    max_concession = max(0.0, mid_credit - worst_credit) * concession_pct
    target_credit = mid_credit - max_concession if enabled else mid_credit
    min_credit = _min_acceptable_credit(order, config or {})
    max_debit = _max_acceptable_debit(order, settings)
    price_type = "credit" if mid_credit >= 0 else "debit"
    guardrail = ""

    if price_type == "credit":
        guarded = max(target_credit, min_credit)
        if guarded > target_credit:
            guardrail = "min_credit"
        target_credit = min(mid_credit, guarded)
    else:
        guarded = max(target_credit, -max_debit)
        if guarded > target_credit:
            guardrail = "max_debit"
        target_credit = min(mid_credit, guarded)

    schedule = _schedule(
        start=mid_credit,
        end=target_credit,
        steps=steps,
        unit_qty=unit_qty,
    )
    return {
        "enabled": enabled,
        "price_type": price_type,
        "unit_qty": unit_qty,
        "initial_net_credit": round(mid_credit, 2),
        "worst_net_credit": round(worst_credit, 2),
        "max_acceptable_net_credit": round(target_credit, 2),
        "initial_limit_price": schedule[0]["limit_price"] if schedule else 0.0,
        "max_acceptable_limit_price": schedule[-1]["limit_price"] if schedule else 0.0,
        "min_acceptable_credit": round(min_credit, 2) if price_type == "credit" else None,
        "max_acceptable_debit": round(max_debit, 2) if price_type == "debit" else None,
        "guardrail": guardrail,
        "schedule": schedule,
    }


def initial_limit_price(order: StrategyOrder, *, config: dict[str, Any] | None = None) -> float:
    plan = limit_price_improvement_plan(order, config=config)
    return float(plan.get("initial_limit_price", 0.0))


def _settings(order: StrategyOrder, config: dict[str, Any]) -> dict[str, Any]:
    execution = config.get("execution", {}) if isinstance(config.get("execution"), dict) else {}
    raw = execution.get("limit_price_improvement", {}) if isinstance(execution, dict) else {}
    if not isinstance(raw, dict):
        raw = {}
    metadata_settings = order.metadata.get("limit_price_improvement_settings", {})
    if not isinstance(metadata_settings, dict):
        metadata_settings = {}
    return {
        "enabled": True,
        "steps": 3,
        "max_concession_pct_of_half_spread": 1.0,
        "liquidity_aware": False,
        "tight_spread_pct": 0.03,
        "wide_spread_pct": 0.10,
        **raw,
        **metadata_settings,
    }


def _liquidity_adjusted_concession(order: StrategyOrder, settings: dict[str, Any]) -> float:
    configured = max(0.0, min(float(settings.get("max_concession_pct_of_half_spread", 1.0) or 0.0), 1.0))
    if not bool(settings.get("liquidity_aware", False)) or not order.legs:
        return configured
    average_spread = sum(max(0.0, leg.contract.spread_pct()) for leg in order.legs) / len(order.legs)
    tight = max(0.0, float(settings.get("tight_spread_pct", 0.03)))
    wide = max(tight, float(settings.get("wide_spread_pct", 0.10)))
    if average_spread <= tight:
        return min(configured, 0.35)
    if average_spread >= wide:
        return configured
    progress = (average_spread - tight) / (wide - tight)
    return round(min(configured, 0.35 + ((configured - 0.35) * progress)), 6)


def _unit_qty(order: StrategyOrder) -> int:
    quantities = [abs(int(leg.qty)) for leg in order.legs if int(leg.qty) != 0]
    if not quantities:
        return 1
    return max(1, reduce(gcd, quantities))


def _worst_package_credit(order: StrategyOrder) -> float:
    return round(sum(_leg_cashflow_at_worst_price(leg) for leg in order.legs), 2)


def _leg_cashflow_at_worst_price(leg: OrderLeg) -> float:
    contract = leg.contract
    if leg.side == "sell_to_open":
        price = contract.bid if contract.bid > 0 else contract.mid_price()
        sign = 1.0
    else:
        price = contract.ask if contract.ask > 0 else contract.mid_price()
        sign = -1.0
    return round(sign * float(price) * 100.0 * max(1, int(leg.qty)), 2)


def _min_acceptable_credit(order: StrategyOrder, config: dict[str, Any]) -> float:
    strategy_cfg = _strategy_config(order, config)
    min_credit = _safe_float(strategy_cfg.get("min_net_credit"), 0.0)
    min_credit_to_width = _safe_float(strategy_cfg.get("min_credit_to_width"), 0.0)
    if min_credit_to_width > 0 and order.max_loss > 0:
        estimated_width = float(order.max_loss) + max(float(order.net_opening_credit), 0.0)
        min_credit = max(min_credit, estimated_width * min_credit_to_width)
    return max(0.0, min_credit)


def _max_acceptable_debit(order: StrategyOrder, settings: dict[str, Any]) -> float:
    max_loss = max(0.0, float(order.max_loss))
    if max_loss <= 0:
        return abs(float(order.net_opening_credit))
    pct = _safe_float(settings.get("max_debit_pct_of_max_loss"), 1.0)
    pct = max(0.0, min(pct, 1.0))
    return max(abs(float(order.net_opening_credit)), max_loss * pct)


def _strategy_config(order: StrategyOrder, config: dict[str, Any]) -> dict[str, Any]:
    strategies = config.get("strategies", {}) if isinstance(config.get("strategies"), dict) else {}
    raw = strategies.get(order.strategy_name, {}) if isinstance(strategies, dict) else {}
    return raw if isinstance(raw, dict) else {}


def _schedule(*, start: float, end: float, steps: int, unit_qty: int) -> list[dict[str, Any]]:
    if steps <= 1 or round(start, 4) == round(end, 4):
        return [_schedule_item(1, start, unit_qty)]
    items = []
    seen: set[tuple[float, float]] = set()
    for index in range(steps):
        fraction = index / (steps - 1)
        credit = start + ((end - start) * fraction)
        item = _schedule_item(index + 1, credit, unit_qty)
        key = (item["limit_price"], item["net_credit"])
        if key not in seen:
            items.append(item)
            seen.add(key)
    return items


def _schedule_item(attempt: int, net_credit: float, unit_qty: int) -> dict[str, Any]:
    signed_price = round(net_credit / (100.0 * max(1, unit_qty)), 4)
    return {
        "attempt": attempt,
        "limit_price": round(abs(signed_price), 2),
        "signed_limit_price": signed_price,
        "net_credit": round(net_credit, 2),
    }


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
