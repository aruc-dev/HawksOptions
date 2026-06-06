"""Run a scan across the configured watchlist."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from ai.news_client import fetch_headlines
from ai.news_gate import evaluate_news_gate
from ai.openai_client import critique_with_llm, safe_review_result
from ai.trade_idea_critic import critique_trade
from core.contract_selector import select_iron_condor, select_vertical_spread
from core.file_lock import atomic_write_text
from core.models import OptionContract, OrderLeg, StrategyContext, StrategyOrder
from core.order_executor import execute_order, persist_open_order, position_from_order
from core.quote_freshness import quote_timestamp
from core.risk_manager import pre_trade_check
from scheduler.common import build_context, configured_underlyings, current_positions, load_runtime, refresh_positions
from strategies import build_enabled_strategies
from strategies.earnings_calendar_scanner import scan_earnings_calendar_candidates, scan_volatility_crush_iron_condor_candidates
from strategies.selection import build_candidate, rank_candidates

_LOGGER = logging.getLogger(__name__)

_EARNINGS_BLACKOUT_STRATEGIES = {
    "broken_wing_butterfly",
    "butterfly",
    "cash_secured_put",
    "collar",
    "covered_call",
    "diagonal_spread",
    "iron_condor",
    "tail_risk_hedge",
    "vertical_spread",
}
_EVENT_RISK_STRATEGIES = {
    "broken_wing_butterfly",
    "butterfly",
    "calendar_spread",
    "cash_secured_put",
    "collar",
    "covered_call",
    "diagonal_spread",
    "iron_condor",
    "vertical_spread",
}
_TECHNICAL_REGIME_STRATEGIES = {
    "cash_secured_put",
    "covered_call",
    "iron_condor",
    "vertical_spread",
}
_IV_REALIZED_STRATEGIES = {
    "broken_wing_butterfly",
    "iron_condor",
    "vertical_spread",
}


def _remaining_entry_slots(config: dict[str, Any], open_position_count: int) -> int:
    max_open = int(config.get("account", {}).get("max_open_strategies", 8))
    return max(0, max_open - open_position_count)


def _ai_section(config: dict[str, Any]) -> dict[str, Any]:
    section = config.get("ai") or {}
    return section if isinstance(section, dict) else {}


def _subsection(section: dict[str, Any], key: str) -> dict[str, Any]:
    inner = section.get(key) or {}
    return inner if isinstance(inner, dict) else {}


def _order_summary(order: StrategyOrder) -> dict[str, Any]:
    """Compact, JSON-safe view of an order for the LLM prompt.

    The LLM does not need the full order graph — just the fields that
    let it reason about size, structure, and timing.
    """
    risk_reward_ratio = (
        round(float(order.max_profit) / float(order.max_loss), 6)
        if float(order.max_loss) > 0
        else None
    )
    return {
        "strategy_name": order.strategy_name,
        "underlying": order.underlying,
        "max_loss": round(float(order.max_loss), 2),
        "max_profit": round(float(order.max_profit), 2),
        "required_buying_power": round(float(order.required_buying_power), 2),
        "risk_reward_ratio": risk_reward_ratio,
        "net_opening_credit": float(order.net_opening_credit),
        "iv_rank": round(float(order.iv_rank), 2),
        "min_dte": int(order.min_dte),
        "exit_rules": {
            "profit_take_pct": float(order.profit_take_pct),
            "loss_stop_multiple": float(order.loss_stop_multiple),
            "roll_threshold_delta": order.roll_threshold_delta,
            "swing_only": bool(order.swing_only),
        },
        "next_earnings_date": (
            order.next_earnings_date.isoformat() if order.next_earnings_date else None
        ),
        "ex_dividend_date": (
            order.ex_dividend_date.isoformat() if order.ex_dividend_date else None
        ),
        "legs": [
            {
                "contract_symbol": leg.contract.contract_symbol,
                "side": leg.side,
                "qty": int(leg.qty),
                "option_type": leg.contract.option_type,
                "strike": float(leg.contract.strike),
                "expiration": leg.contract.expiration.isoformat(),
                "bid": float(leg.contract.bid),
                "ask": float(leg.contract.ask),
                "mid_price": leg.contract.mid_price(),
                "spread_pct": leg.contract.spread_pct() if leg.contract.mid_price() > 0 else None,
                "open_interest": int(leg.contract.open_interest),
                "volume": int(leg.contract.volume),
                "implied_volatility": float(leg.contract.implied_volatility),
                "delta": leg.contract.delta,
                "theta": leg.contract.theta,
                "vega": leg.contract.vega,
                "gamma": leg.contract.gamma,
                "underlying_price": float(leg.contract.underlying_price),
            }
            for leg in order.legs
        ],
    }


def build_pre_ai_feature_packet(
    order: StrategyOrder,
    *,
    structural_severity: str,
    structural_concerns: list[str] | None = None,
    risk_warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Deterministic packet passed to optional external AI reviewers."""
    return {
        "schema_version": 1,
        "order": _order_summary(order),
        "deterministic_review": {
            "structural_severity": str(structural_severity or "none"),
            "structural_concerns": list(structural_concerns or []),
            "risk_warnings": list(risk_warnings or []),
        },
        "selection": dict(order.metadata.get("selection", {})),
    }


def _safe_news_review(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "veto": bool(value.get("veto", False)),
        "reason": str(value.get("reason", "")),
        "score": value.get("score"),
        "matched_headlines": [
            str(item)
            for item in value.get("matched_headlines", [])
            if isinstance(item, str) and item.strip()
        ],
    }


def _safe_external_ai_review(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "veto_reason": str(value.get("veto_reason") or ""),
        "news": _safe_news_review(value.get("news")),
        "llm": safe_review_result(value.get("llm")),
    }


def build_ai_disagreement_entry(
    order: StrategyOrder,
    *,
    stage: str,
    disagreement_type: str,
    deterministic_decision: str,
    ai_decision: str,
    structural_severity: str,
    reasons: list[str] | None = None,
    external: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Read-only audit record for deterministic-vs-AI review differences."""
    feature_packet = order.metadata.get("pre_ai_feature_packet")
    if not isinstance(feature_packet, dict):
        feature_packet = build_pre_ai_feature_packet(
            order,
            structural_severity=structural_severity,
        )
    return {
        "schema_version": 1,
        "type": str(disagreement_type),
        "stage": str(stage),
        "underlying": order.underlying,
        "strategy": order.strategy_name,
        "deterministic_decision": str(deterministic_decision),
        "ai_decision": str(ai_decision),
        "structural_severity": str(structural_severity or "none"),
        "reasons": [str(reason) for reason in (reasons or [])],
        "feature_packet": feature_packet,
        "external_review": _safe_external_ai_review(external),
    }


def _ai_disagreement_summary(disagreements: list[dict[str, Any]]) -> dict[str, Any]:
    by_type: dict[str, int] = {}
    by_strategy: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    for item in disagreements:
        disagreement_type = str(item.get("type", "unknown"))
        strategy = str(item.get("strategy", "unknown"))
        stage = str(item.get("stage", "unknown"))
        by_type[disagreement_type] = by_type.get(disagreement_type, 0) + 1
        by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
        by_stage[stage] = by_stage.get(stage, 0) + 1
    return {
        "total": len(disagreements),
        "by_type": dict(sorted(by_type.items(), key=lambda item: (-item[1], item[0]))),
        "by_strategy": dict(sorted(by_strategy.items(), key=lambda item: (-item[1], item[0]))),
        "by_stage": dict(sorted(by_stage.items(), key=lambda item: (-item[1], item[0]))),
    }


def _config_hash(config: dict[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _persist_candidate_scan(
    *,
    paths: dict[str, Path],
    payload: dict[str, Any],
    config: dict[str, Any],
    account: dict[str, Any],
    underlyings: list[dict[str, Any]],
    open_position_count: int,
) -> Path:
    reports_dir = paths["reports_dir"]
    scan_dir = reports_dir / "candidate_scans"
    scan_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    path = scan_dir / f"scan_{payload['as_of']}_{timestamp:%H%M%S%f}Z.json"
    trace = {
        "schema_version": 1,
        "scan_timestamp": timestamp.isoformat(),
        "as_of": payload["as_of"],
        "dry_run": payload["dry_run"],
        "config_hash": _config_hash(config),
        "market_snapshot": {
            "account_equity": account.get("equity"),
            "portfolio_value": account.get("portfolio_value"),
            "cash": account.get("cash"),
            "buying_power": account.get("buying_power"),
            "open_position_count": open_position_count,
            "underlyings": [str(item.get("symbol", "")) for item in underlyings],
        },
        "candidate_count": payload["candidate_count"],
        "accepted_count": payload["accepted_count"],
        "rejected_count": payload["rejected_count"],
        "generated_candidates": payload["ranked_candidates"],
        "ranked_candidates": payload["ranked_candidates"],
        "research_candidates": payload["research_candidates"],
        "research_traces": payload["research_traces"],
        "ai_disagreements": payload["ai_disagreements"],
        "chosen_order": payload["accepted"][0] if payload["accepted"] else None,
        "chosen_orders": payload["accepted"],
        "accepted": payload["accepted"],
        "rejected": payload["rejected"],
        "scan_health": payload["scan_health"],
    }
    atomic_write_text(path, json.dumps(trace, indent=2, sort_keys=True, default=str), lock=False)
    return path


def _research_trace_entry(
    *,
    symbol: str,
    scanner: str,
    enabled: bool,
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "scanner": scanner,
        "enabled": bool(enabled),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


def _persist_research_trace(*, paths: dict[str, Path], payload: dict[str, Any]) -> Path:
    reports_dir = paths["reports_dir"]
    trace_dir = reports_dir / "research_traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    path = trace_dir / f"research_trace_{payload['as_of']}_{timestamp:%H%M%S%f}Z.json"
    trace = {
        "schema_version": 1,
        "as_of": payload["as_of"],
        "dry_run": payload["dry_run"],
        "trace_count": len(payload["research_traces"]),
        "candidate_count": len(payload["research_candidates"]),
        "traces": payload["research_traces"],
    }
    atomic_write_text(path, json.dumps(trace, indent=2, sort_keys=True, default=str), lock=False)
    return path


def _persist_ai_disagreement_log(*, paths: dict[str, Path], payload: dict[str, Any]) -> Path:
    reports_dir = paths["reports_dir"]
    disagreement_dir = reports_dir / "ai_disagreements"
    disagreement_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    path = disagreement_dir / f"ai_disagreements_{payload['as_of']}_{timestamp:%H%M%S%f}Z.json"
    trace = {
        "schema_version": 1,
        "as_of": payload["as_of"],
        "dry_run": payload["dry_run"],
        "summary": _ai_disagreement_summary(payload["ai_disagreements"]),
        "disagreements": payload["ai_disagreements"],
    }
    atomic_write_text(path, json.dumps(trace, indent=2, sort_keys=True, default=str), lock=False)
    return path


def _rejection_summary(rejected: list[dict[str, Any]]) -> dict[str, Any]:
    by_reason: dict[str, int] = {}
    by_strategy: dict[str, int] = {}
    by_stage: dict[str, int] = {}
    for item in rejected:
        strategy = str(item.get("strategy", "unknown"))
        stage = str(item.get("stage", "risk"))
        by_strategy[strategy] = by_strategy.get(strategy, 0) + 1
        by_stage[stage] = by_stage.get(stage, 0) + 1
        for reason in item.get("reasons", []) or []:
            reason_text = str(reason)
            by_reason[reason_text] = by_reason.get(reason_text, 0) + 1
    return {
        "total_rejected": len(rejected),
        "by_reason": dict(sorted(by_reason.items(), key=lambda item: (-item[1], item[0]))),
        "by_strategy": dict(sorted(by_strategy.items(), key=lambda item: (-item[1], item[0]))),
        "by_stage": dict(sorted(by_stage.items(), key=lambda item: (-item[1], item[0]))),
    }


def _as_utc_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc).replace(hour=16)


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _missing_greeks_by_field(chain: list[OptionContract]) -> dict[str, int]:
    fields = ("delta", "theta", "vega", "gamma")
    return {
        field: sum(1 for contract in chain if getattr(contract, field) is None)
        for field in fields
    }


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _safe_spread_pct(contract: OptionContract) -> float | None:
    spread = contract.spread_pct()
    return None if spread == float("inf") else round(float(spread), 6)


def _contract_near_miss_summary(contract: OptionContract, *, as_of: date | datetime) -> dict[str, Any]:
    return {
        "contract_symbol": contract.contract_symbol,
        "option_type": contract.option_type,
        "strike": float(contract.strike),
        "expiration": contract.expiration.isoformat(),
        "dte": contract.days_to_expiration(as_of),
        "bid": float(contract.bid),
        "ask": float(contract.ask),
        "mid_price": contract.mid_price(),
        "spread_pct": _safe_spread_pct(contract),
        "open_interest": int(contract.open_interest),
        "volume": int(contract.volume),
        "delta": contract.delta,
        "implied_volatility": float(contract.implied_volatility),
    }


def _nearest_contracts(
    contracts: list[OptionContract],
    *,
    target_delta: float,
    target_dte: int,
    as_of: date | datetime,
    limit: int = 3,
) -> list[dict[str, Any]]:
    candidates = [contract for contract in contracts if contract.delta is not None]
    candidates.sort(
        key=lambda contract: (
            abs(abs(float(contract.delta or 0.0)) - abs(target_delta)),
            abs(contract.days_to_expiration(as_of) - target_dte),
            contract.spread_pct(),
        )
    )
    return [_contract_near_miss_summary(contract, as_of=as_of) for contract in candidates[:limit]]


def _leg_quality_summary(contracts: list[OptionContract]) -> dict[str, Any]:
    spreads = [
        spread
        for spread in (_safe_spread_pct(contract) for contract in contracts)
        if spread is not None
    ]
    return {
        "min_open_interest": min((int(contract.open_interest) for contract in contracts), default=0),
        "min_volume": min((int(contract.volume) for contract in contracts), default=0),
        "avg_spread_pct": round(sum(spreads) / len(spreads), 6) if spreads else None,
    }


def _vertical_option_type(strategy: Any, context: StrategyContext) -> str | None:
    variant = str(strategy.params.get("variant", "bull_put_credit"))
    if variant == "auto":
        trend = context.trend_20d
        if trend is None:
            trend = context.underlying.get("trend_20d", 0.0)
        variant = "bull_put_credit" if float(trend or 0.0) >= 0 else "bear_call_credit"
    if variant in {"bull_put_credit", "bullish"}:
        return "put"
    if variant in {"bear_call_credit", "bearish"}:
        return "call"
    return None


def _vertical_generation_reasons(strategy: Any, context: StrategyContext, details: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    option_type = _vertical_option_type(strategy, context)
    details["variant"] = str(strategy.params.get("variant", "bull_put_credit"))
    details["option_type"] = option_type
    if option_type is None:
        return ["unsupported_vertical_variant"]
    if not strategy.volatility_surface_filter_passes(context, option_type=option_type):
        _append_unique(reasons, f"{option_type}_surface")
    filtered = strategy.filtered_chain(context, option_type)
    details["filtered_contract_count"] = len(filtered)
    pair = select_vertical_spread(
        filtered,
        short_delta=float(strategy.params.get("short_delta", -0.25)),
        long_delta=float(strategy.params.get("long_delta", -0.10)),
        option_type=option_type,
        target_dte=int(strategy.params.get("target_dte", 35)),
        as_of=context.as_of,
    )
    if not filtered:
        _append_unique(reasons, f"no_filtered_{option_type}s")
    elif pair is None:
        _append_unique(reasons, "no_delta_pair")
        target_dte = int(strategy.params.get("target_dte", 35))
        details["near_miss"] = {
            "structure": "vertical_spread",
            "option_type": option_type,
            "filtered_contract_count": len(filtered),
            "nearest_short_candidates": _nearest_contracts(
                filtered,
                target_delta=float(strategy.params.get("short_delta", -0.25)),
                target_dte=target_dte,
                as_of=context.as_of,
            ),
            "nearest_long_candidates": _nearest_contracts(
                filtered,
                target_delta=float(strategy.params.get("long_delta", -0.10)),
                target_dte=target_dte,
                as_of=context.as_of,
            ),
        }
    else:
        short_leg, long_leg = pair
        width = abs(short_leg.strike - long_leg.strike) * 100.0
        credit = round((short_leg.mid_price() - long_leg.mid_price()) * 100.0, 2)
        legs = [
            OrderLeg(contract=short_leg, side="sell_to_open", qty=1),
            OrderLeg(contract=long_leg, side="buy_to_open", qty=1),
        ]
        modeled_roundtrip_cost = strategy.modeled_entry_cost(legs) * 2.0
        details["selected_credit"] = credit
        details["selected_width"] = width
        details["near_miss"] = {
            "structure": "vertical_spread",
            "option_type": option_type,
            "credit": credit,
            "width": width,
            "max_loss": round(max(0.0, width - credit), 2),
            "credit_to_width": round(credit / width, 6) if width > 0 else None,
            "modeled_roundtrip_cost": round(modeled_roundtrip_cost, 4),
            "required_credit_for_roundtrip": round(
                modeled_roundtrip_cost * float(strategy.params.get("min_credit_to_roundtrip_cost", 0.0)),
                4,
            ),
            "legs": [
                {"side": "sell_to_open", **_contract_near_miss_summary(short_leg, as_of=context.as_of)},
                {"side": "buy_to_open", **_contract_near_miss_summary(long_leg, as_of=context.as_of)},
            ],
            **_leg_quality_summary([short_leg, long_leg]),
        }
        if credit <= 0:
            _append_unique(reasons, "non_positive_credit")
        if width <= credit:
            _append_unique(reasons, "invalid_spread_width")
        if not strategy.credit_quality_passes(credit=credit, width=width):
            _append_unique(reasons, "credit_quality")
    return reasons


def _iron_condor_generation_reasons(strategy: Any, context: StrategyContext, details: dict[str, Any]) -> list[str]:
    reasons: list[str] = []
    min_iv_rank = float(strategy.params.get("min_iv_rank", 0.0))
    details["min_iv_rank"] = min_iv_rank
    if min_iv_rank > 0 and context.iv_rank < min_iv_rank:
        _append_unique(reasons, "iv_rank_below_min")
    if not strategy.volatility_surface_filter_passes(context, option_type="put"):
        _append_unique(reasons, "put_surface")
    if not strategy.volatility_surface_filter_passes(context, option_type="call"):
        _append_unique(reasons, "call_surface")
    max_atr = float(strategy.params.get("max_atr_pct", 0.03))
    details["max_atr_pct"] = max_atr
    if context.atr_pct > max_atr:
        _append_unique(reasons, "atr_above_max")
    contracts = select_iron_condor(
        context.chain,
        put_short_delta=float(strategy.params.get("put_short_delta", -0.18)),
        put_long_delta=float(strategy.params.get("put_long_delta", -0.08)),
        call_short_delta=float(strategy.params.get("call_short_delta", 0.18)),
        call_long_delta=float(strategy.params.get("call_long_delta", 0.08)),
        target_dte=int(strategy.params.get("target_dte", 40)),
        as_of=context.as_of,
    )
    if contracts is None:
        _append_unique(reasons, "no_iron_condor_structure")
        target_dte = int(strategy.params.get("target_dte", 40))
        puts = [contract for contract in context.chain if contract.option_type == "put"]
        calls = [contract for contract in context.chain if contract.option_type == "call"]
        details["near_miss"] = {
            "structure": "iron_condor",
            "nearest_put_short_candidates": _nearest_contracts(
                puts,
                target_delta=float(strategy.params.get("put_short_delta", -0.18)),
                target_dte=target_dte,
                as_of=context.as_of,
            ),
            "nearest_put_long_candidates": _nearest_contracts(
                puts,
                target_delta=float(strategy.params.get("put_long_delta", -0.08)),
                target_dte=target_dte,
                as_of=context.as_of,
            ),
            "nearest_call_short_candidates": _nearest_contracts(
                calls,
                target_delta=float(strategy.params.get("call_short_delta", 0.18)),
                target_dte=target_dte,
                as_of=context.as_of,
            ),
            "nearest_call_long_candidates": _nearest_contracts(
                calls,
                target_delta=float(strategy.params.get("call_long_delta", 0.08)),
                target_dte=target_dte,
                as_of=context.as_of,
            ),
        }
        return reasons
    short_put, long_put, short_call, long_call = contracts
    credit = round(
        (short_put.mid_price() + short_call.mid_price() - long_put.mid_price() - long_call.mid_price()) * 100.0,
        2,
    )
    put_width = abs(short_put.strike - long_put.strike) * 100.0
    call_width = abs(long_call.strike - short_call.strike) * 100.0
    width = max(put_width, call_width)
    max_loss = round(width - credit, 2)
    details["selected_credit"] = credit
    details["selected_width"] = width
    details["selected_max_loss"] = max_loss
    if credit <= 0:
        _append_unique(reasons, "non_positive_credit")
    if max_loss <= 0:
        _append_unique(reasons, "non_positive_max_loss")
    min_credit_to_width = float(strategy.params.get("min_credit_to_width", 0.0))
    details["min_credit_to_width"] = min_credit_to_width
    if width > 0 and min_credit_to_width > 0 and credit / width < min_credit_to_width:
        _append_unique(reasons, "credit_to_width")
    if not strategy.credit_quality_passes(credit=credit, width=width):
        _append_unique(reasons, "credit_quality")
    legs = [
        OrderLeg(contract=short_put, side="sell_to_open", qty=1),
        OrderLeg(contract=long_put, side="buy_to_open", qty=1),
        OrderLeg(contract=short_call, side="sell_to_open", qty=1),
        OrderLeg(contract=long_call, side="buy_to_open", qty=1),
    ]
    roundtrip_cost = strategy.modeled_entry_cost(legs) * 2.0
    details["modeled_roundtrip_cost"] = round(roundtrip_cost, 4)
    details["near_miss"] = {
        "structure": "iron_condor",
        "credit": credit,
        "width": width,
        "max_loss": max_loss,
        "credit_to_width": round(credit / width, 6) if width > 0 else None,
        "modeled_roundtrip_cost": round(roundtrip_cost, 4),
        "required_credit_for_roundtrip": round(
            roundtrip_cost * float(strategy.params.get("min_credit_to_roundtrip_cost", 0.0)),
            4,
        ),
        "legs": [
            {"side": "sell_to_open", **_contract_near_miss_summary(short_put, as_of=context.as_of)},
            {"side": "buy_to_open", **_contract_near_miss_summary(long_put, as_of=context.as_of)},
            {"side": "sell_to_open", **_contract_near_miss_summary(short_call, as_of=context.as_of)},
            {"side": "buy_to_open", **_contract_near_miss_summary(long_call, as_of=context.as_of)},
        ],
        **_leg_quality_summary([short_put, long_put, short_call, long_call]),
    }
    if not strategy.cost_adjusted_credit_passes(credit=credit, legs=legs):
        _append_unique(reasons, "cost_adjusted_credit")
    return reasons


def _strategy_generation_diagnostics(strategy: Any, context: StrategyContext) -> dict[str, Any]:
    """Explain why an enabled strategy did not produce a candidate order."""
    reasons: list[str] = []
    details: dict[str, Any] = {}
    if not strategy.enabled():
        _append_unique(reasons, "disabled")
    if not strategy.allowed_for_underlying(context):
        _append_unique(reasons, "not_allowed")
    if strategy.name in _EARNINGS_BLACKOUT_STRATEGIES and strategy.in_earnings_blackout(context):
        _append_unique(reasons, "earnings_blackout")
    if strategy.name in _EVENT_RISK_STRATEGIES and not strategy.event_risk_filter_passes(context):
        _append_unique(reasons, "event_risk")
    if strategy.name in _TECHNICAL_REGIME_STRATEGIES and not strategy.technical_regime_filter_passes(context):
        _append_unique(reasons, "technical_regime")
    if strategy.name in _IV_REALIZED_STRATEGIES and not strategy.implied_realized_filter_passes(context):
        _append_unique(reasons, "iv_realized")
    try:
        if strategy.order_quantity(context) <= 0:
            _append_unique(reasons, "zero_quantity")
    except Exception as exc:
        details["quantity_error"] = f"{type(exc).__name__}: {exc}"
    if strategy.name == "vertical_spread":
        reasons.extend(reason for reason in _vertical_generation_reasons(strategy, context, details) if reason not in reasons)
    elif strategy.name == "iron_condor":
        reasons.extend(reason for reason in _iron_condor_generation_reasons(strategy, context, details) if reason not in reasons)
    if not reasons:
        reasons.append("strategy_returned_none")
    return {
        "strategy": str(strategy.name),
        "reasons": reasons,
        "details": details,
    }


def _symbol_scan_health(
    context: StrategyContext,
    *,
    gates: dict[str, Any],
    as_of: date | datetime,
) -> dict[str, Any]:
    chain = list(context.chain)
    dtes = [contract.days_to_expiration(as_of) for contract in chain]
    expirations = {contract.expiration.isoformat() for contract in chain}
    max_age = _optional_positive_float(gates.get("max_quote_age_seconds"))
    as_dt = _as_utc_datetime(as_of)
    missing_greeks = _missing_greeks_by_field(chain)
    max_spread = _optional_positive_float(gates.get("max_bid_ask_spread_pct"))
    stale_quotes = 0
    missing_timestamps = 0
    stale_fallbacks = 0
    invalid_quotes = 0
    wide_quotes = 0
    for contract in chain:
        mid_price = contract.mid_price()
        if contract.bid <= 0 or contract.ask <= 0 or mid_price <= 0:
            invalid_quotes += 1
        elif max_spread is not None and contract.spread_pct() > max_spread:
            wide_quotes += 1
        if str(contract.meta.get("lifecycle_state", "")).lower() == "stale_quote_fallback":
            stale_fallbacks += 1
        timestamp = quote_timestamp(contract)
        if timestamp is None:
            missing_timestamps += 1
            continue
        if max_age is not None and (as_dt - timestamp).total_seconds() > max_age:
            stale_quotes += 1
    return {
        "symbol": context.underlying["symbol"],
        "chain_available": bool(chain),
        "contract_count": len(chain),
        "expiration_count": len(expirations),
        "min_dte": min(dtes) if dtes else None,
        "max_dte": max(dtes) if dtes else None,
        "missing_greeks_contract_count": sum(
            1 for contract in chain if any(getattr(contract, field) is None for field in missing_greeks)
        ),
        "missing_greeks_by_field": missing_greeks,
        "missing_quote_timestamps": missing_timestamps,
        "stale_quote_count": stale_quotes,
        "stale_quote_fallback_count": stale_fallbacks,
        "invalid_quote_count": invalid_quotes,
        "wide_quote_count": wide_quotes,
        "candidate_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
        "top_rejection_reasons": {},
        "strategy_diagnostics": [],
        "top_strategy_blockers": {},
        "near_misses": [],
    }


def _unavailable_symbol_scan_health(*, symbol: str, reason: str) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "chain_available": False,
        "contract_count": 0,
        "expiration_count": 0,
        "min_dte": None,
        "max_dte": None,
        "missing_greeks_contract_count": 0,
        "missing_greeks_by_field": {"delta": 0, "theta": 0, "vega": 0, "gamma": 0},
        "missing_quote_timestamps": 0,
        "stale_quote_count": 0,
        "stale_quote_fallback_count": 0,
        "invalid_quote_count": 0,
        "wide_quote_count": 0,
        "candidate_count": 0,
        "accepted_count": 0,
        "rejected_count": 0,
        "top_rejection_reasons": {},
        "strategy_diagnostics": [],
        "top_strategy_blockers": {},
        "near_misses": [],
        "context_available": False,
        "context_error": reason,
    }


def _scan_health_summary(
    *,
    symbol_health: list[dict[str, Any]],
    ranked_candidates: list[dict[str, Any]],
    accepted: list[dict[str, Any]],
    rejected: list[dict[str, Any]],
) -> dict[str, Any]:
    by_symbol = {str(item["symbol"]): dict(item) for item in symbol_health}
    rejection_reasons_by_symbol: dict[str, dict[str, int]] = {}
    strategy_blockers: dict[str, int] = {}
    near_misses: list[dict[str, Any]] = []
    for candidate in ranked_candidates:
        symbol = str(candidate.get("underlying", ""))
        if symbol in by_symbol:
            by_symbol[symbol]["candidate_count"] += 1
    for item in accepted:
        symbol = str(item.get("underlying", ""))
        if symbol in by_symbol:
            by_symbol[symbol]["accepted_count"] += 1
    for item in rejected:
        symbol = str(item.get("underlying", ""))
        if symbol in by_symbol:
            by_symbol[symbol]["rejected_count"] += 1
        bucket = rejection_reasons_by_symbol.setdefault(symbol, {})
        for reason in item.get("reasons", []) or []:
            reason_text = str(reason)
            bucket[reason_text] = bucket.get(reason_text, 0) + 1
    for symbol, reasons in rejection_reasons_by_symbol.items():
        if symbol not in by_symbol:
            continue
        by_symbol[symbol]["top_rejection_reasons"] = dict(
            sorted(reasons.items(), key=lambda item: (-item[1], item[0]))[:5]
        )
    for item in by_symbol.values():
        symbol_blockers: dict[str, int] = {}
        for diagnostic in item.get("strategy_diagnostics", []) or []:
            for reason in diagnostic.get("reasons", []) or []:
                reason_text = str(reason)
                symbol_blockers[reason_text] = symbol_blockers.get(reason_text, 0) + 1
                strategy_blockers[reason_text] = strategy_blockers.get(reason_text, 0) + 1
            near_miss = (diagnostic.get("details") or {}).get("near_miss")
            if isinstance(near_miss, dict):
                row = {
                    "symbol": str(item["symbol"]),
                    "strategy": str(diagnostic.get("strategy", "")),
                    "reasons": [str(reason) for reason in diagnostic.get("reasons", [])],
                    **near_miss,
                }
                item.setdefault("near_misses", []).append(row)
                near_misses.append(row)
        item["top_strategy_blockers"] = dict(
            sorted(symbol_blockers.items(), key=lambda value: (-value[1], value[0]))[:5]
        )
    top_reasons = _rejection_summary(rejected)["by_reason"]
    symbols = sorted(by_symbol)
    stale_symbols = [
        symbol
        for symbol, item in by_symbol.items()
        if item["stale_quote_count"] or item["stale_quote_fallback_count"]
    ]
    missing_greek_symbols = [
        symbol
        for symbol, item in by_symbol.items()
        if item["missing_greeks_contract_count"]
    ]
    unavailable_symbols = [
        symbol
        for symbol, item in by_symbol.items()
        if not item["chain_available"]
    ]
    return {
        "symbols_scanned": symbols,
        "symbol_count": len(symbols),
        "chain_unavailable_symbols": sorted(unavailable_symbols),
        "stale_data_symbols": sorted(stale_symbols),
        "missing_greeks_symbols": sorted(missing_greek_symbols),
        "candidate_count": len(ranked_candidates),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "top_rejection_reasons": dict(list(top_reasons.items())[:10]),
        "top_strategy_blockers": dict(
            sorted(strategy_blockers.items(), key=lambda item: (-item[1], item[0]))[:10]
        ),
        "near_miss_count": len(near_misses),
        "near_misses": near_misses[:20],
        "by_symbol": [by_symbol[symbol] for symbol in symbols],
    }


def _persist_scan_health_report(*, paths: dict[str, Path], payload: dict[str, Any]) -> Path:
    reports_dir = paths["reports_dir"]
    health_dir = reports_dir / "scan_health"
    health_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    path = health_dir / f"scan_health_{payload['as_of']}_{timestamp:%H%M%S%f}Z.md"
    health = payload["scan_health"]
    lines = [
        "# HawksOptions Scan Health",
        "",
        f"- As of: {payload['as_of']}",
        f"- Dry run: {payload['dry_run']}",
        f"- Symbols scanned: {health['symbol_count']}",
        f"- Candidates: {health['candidate_count']}",
        f"- Accepted: {health['accepted_count']}",
        f"- Rejected: {health['rejected_count']}",
        f"- Chain unavailable symbols: {', '.join(health['chain_unavailable_symbols']) or 'none'}",
        f"- Stale data symbols: {', '.join(health['stale_data_symbols']) or 'none'}",
        f"- Missing Greeks symbols: {', '.join(health['missing_greeks_symbols']) or 'none'}",
        "",
        "## Top Rejection Reasons",
        "",
    ]
    if health["top_rejection_reasons"]:
        lines.extend(f"- {reason}: {count}" for reason, count in health["top_rejection_reasons"].items())
    else:
        lines.append("- none")
    lines.extend(["", "## Top Strategy Blockers", ""])
    if health["top_strategy_blockers"]:
        lines.extend(f"- {reason}: {count}" for reason, count in health["top_strategy_blockers"].items())
    else:
        lines.append("- none")
    lines.extend(["", "## Near-Miss Structures", ""])
    if health["near_misses"]:
        for item in health["near_misses"][:10]:
            credit = item.get("credit")
            width = item.get("width")
            cost = item.get("modeled_roundtrip_cost")
            credit_text = f"${float(credit):.2f}" if isinstance(credit, (int, float)) else "n/a"
            width_text = f"${float(width):.2f}" if isinstance(width, (int, float)) else "n/a"
            cost_text = f"${float(cost):.2f}" if isinstance(cost, (int, float)) else "n/a"
            lines.append(
                f"- {item['symbol']} {item['strategy']}: {', '.join(item.get('reasons', []))}; "
                f"credit {credit_text}, width {width_text}, modeled roundtrip {cost_text}"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## By Symbol", ""])
    for item in health["by_symbol"]:
        lines.extend(
            [
                f"### {item['symbol']}",
                "",
                f"- Chain available: {item['chain_available']}",
                f"- Contracts: {item['contract_count']}",
                f"- Expirations: {item['expiration_count']}",
                f"- DTE range: {item['min_dte']} to {item['max_dte']}",
                f"- Missing Greeks contracts: {item['missing_greeks_contract_count']}",
                f"- Missing quote timestamps: {item['missing_quote_timestamps']}",
                f"- Stale quotes: {item['stale_quote_count']}",
                f"- Stale quote fallbacks: {item['stale_quote_fallback_count']}",
                f"- Invalid quotes: {item['invalid_quote_count']}",
                f"- Wide quotes: {item['wide_quote_count']}",
                f"- Candidates: {item['candidate_count']}",
                f"- Accepted: {item['accepted_count']}",
                f"- Rejected: {item['rejected_count']}",
                f"- Top strategy blockers: {', '.join(f'{reason} ({count})' for reason, count in item['top_strategy_blockers'].items()) or 'none'}",
                f"- Near misses: {len(item.get('near_misses', []))}",
                "",
            ]
        )
        if item.get("strategy_diagnostics"):
            lines.append("Strategy diagnostics:")
            for diagnostic in item["strategy_diagnostics"]:
                lines.append(
                    f"- {diagnostic['strategy']}: {', '.join(diagnostic['reasons']) or 'none'}"
                )
            lines.append("")
    lines.extend(["```json", json.dumps(health, indent=2, sort_keys=True), "```", ""])
    atomic_write_text(path, "\n".join(lines), lock=False)
    return path


def _persist_rejection_report(*, paths: dict[str, Path], payload: dict[str, Any]) -> Path:
    reports_dir = paths["reports_dir"]
    rejection_dir = reports_dir / "rejections"
    rejection_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    path = rejection_dir / f"rejections_{payload['as_of']}_{timestamp:%H%M%S%f}Z.md"
    summary = _rejection_summary(payload["rejected"])
    lines = [
        "# HawksOptions Rejection Summary",
        "",
        f"- As of: {payload['as_of']}",
        f"- Dry run: {payload['dry_run']}",
        f"- Candidates: {payload['candidate_count']}",
        f"- Accepted: {payload['accepted_count']}",
        f"- Rejected: {payload['rejected_count']}",
        "",
        "## By Reason",
        "",
    ]
    lines.extend(f"- {reason}: {count}" for reason, count in summary["by_reason"].items())
    lines.extend(["", "## By Strategy", ""])
    lines.extend(f"- {strategy}: {count}" for strategy, count in summary["by_strategy"].items())
    lines.extend(["", "## By Stage", ""])
    lines.extend(f"- {stage}: {count}" for stage, count in summary["by_stage"].items())
    lines.extend(["", "```json", json.dumps(summary, indent=2, sort_keys=True), "```", ""])
    atomic_write_text(path, "\n".join(lines), lock=False)
    return path


def evaluate_external_ai(
    order: StrategyOrder,
    *,
    config: dict[str, Any],
    structural_severity: str,
    fetcher=fetch_headlines,
    llm=critique_with_llm,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run optional NewsAPI + OpenAI checks for ``order``.

    Returns a dict with:
      * ``veto_reason``: non-empty string if the external checks veto
        the trade. Empty when no external veto is raised.
      * ``news``: the news-gate result, or ``None`` if disabled.
      * ``llm``: the LLM critic result, or ``None`` if disabled.

    Veto-only contract:
      * A ``major`` from the structural critic is never relaxed by
        this function — the caller decides what to do with it. This
        function only *adds* veto reasons.
      * News gate vetoes only when the keyword evaluator returns
        ``veto: True`` (configurable threshold).
      * LLM vetoes only on ``severity == "major"``. ``minor`` is
        attached as a note but does not block.
    """
    environ = env if env is not None else os.environ
    ai_cfg = _ai_section(config)
    news_cfg = _subsection(ai_cfg, "news_gate")
    critic_cfg = _subsection(ai_cfg, "trade_idea_critic")

    result: dict[str, Any] = {"veto_reason": "", "news": None, "llm": None}
    if structural_severity == "major":
        return result

    # ---- News gate ------------------------------------------------------
    # The news gate is independent of the master ``ai.enabled`` switch:
    # the keyword evaluator is deterministic and config-gated, and live
    # headlines are only fetched when NEWS_API_KEY is set.
    if news_cfg.get("enabled", False):
        headlines: list[str] = []
        news_key = (environ.get("NEWS_API_KEY") or "").strip()
        if news_key:
            try:
                headlines = fetcher(order.underlying, api_key=news_key)
            except Exception as exc:  # pragma: no cover — fetcher is bounded
                _LOGGER.warning("news fetch failed for %s: %s", order.underlying, exc)
                headlines = []
        threshold = float(news_cfg.get("veto_threshold", 0.7))
        news_result = evaluate_news_gate(headlines, veto_threshold=threshold)
        result["news"] = news_result
        if news_result.get("veto"):
            reason = news_result.get("reason") or "news risk"
            result["veto_reason"] = f"news_gate:{reason}"
            return result

    # ---- LLM critic -----------------------------------------------------
    # Only runs when AI is globally enabled, the provider is openai,
    # the trade-idea critic is configured to veto on major concerns,
    # and OPENAI_API_KEY is set.
    if not ai_cfg.get("enabled", False):
        return result
    if str(ai_cfg.get("provider", "")).lower() != "openai":
        return result
    if str(critic_cfg.get("veto_on", "")).lower() != "major_concern":
        return result
    if not (environ.get("OPENAI_API_KEY") or "").strip():
        return result

    feature_packet = order.metadata.get("pre_ai_feature_packet")
    if not isinstance(feature_packet, dict):
        feature_packet = build_pre_ai_feature_packet(
            order,
            structural_severity=structural_severity,
        )
    try:
        raw_llm_result = llm(
            feature_packet,
            headlines=[h for h in (result.get("news") or {}).get("matched_headlines", [])]
            if isinstance(result.get("news"), dict)
            else [],
            daily_spend_cap_usd=float(ai_cfg.get("daily_spend_cap_usd", 5.0)),
        )
    except Exception as exc:  # pragma: no cover — client is bounded
        _LOGGER.warning("openai critic failed for %s: %s", order.underlying, exc)
        return result

    llm_result = safe_review_result(raw_llm_result)
    result["llm"] = llm_result
    # Veto-only: never override a structural pass into a fail unless
    # the LLM raises a *major* concern. ``minor`` and ``none`` do not
    # block. We never relax an already-major structural concern, but
    # we do not need to: the caller already vetoed on it.
    _ = structural_severity  # kept for future cross-checks
    if str(llm_result.get("severity", "")).lower() == "major":
        concerns = llm_result.get("concerns") or []
        first = concerns[0] if concerns else "major concern"
        result["veto_reason"] = f"llm_critic:{first}"
    return result


def scan_market(*, config: dict[str, Any], as_of: date | None = None, dry_run: bool = True) -> dict[str, Any]:
    as_of = as_of or date.today()
    config, client, paths = load_runtime(config)
    account = client.get_account()
    market_context = _market_context_for_scan(config=config, client=client, as_of=as_of)
    if market_context:
        account = {**account, "market_context": market_context}
    positions = refresh_positions(current_positions(paths), client=client, as_of=as_of)
    strategies = build_enabled_strategies(config)
    underlyings = configured_underlyings(config)
    config = {
        **config,
        "_underlying_metadata": {str(item.get("symbol", "")): item for item in underlyings},
    }
    starting_open_position_count = len(positions)
    candidate_pool = []
    research_candidates = []
    research_traces = []
    ai_disagreements = []
    symbol_health = []
    accepted = []
    rejected = []
    research_section = config.get("research") or {}
    research_cfg = research_section.get("earnings_calendar_spread", {}) if isinstance(research_section, dict) else {}
    vol_crush_cfg = (
        research_section.get("volatility_crush_earnings_iron_condor", {})
        if isinstance(research_section, dict)
        else {}
    )
    for underlying in underlyings:
        try:
            context = build_context(
                config=config,
                client=client,
                underlying=underlying,
                account=account,
                open_positions=positions,
                as_of=as_of,
            )
        except Exception as exc:
            symbol = str(underlying.get("symbol", ""))
            reason = f"context_unavailable:{type(exc).__name__}"
            _LOGGER.warning("scan context unavailable for %s: %s", symbol, exc)
            symbol_health.append(_unavailable_symbol_scan_health(symbol=symbol, reason=str(exc)))
            rejected.append(
                {
                    "underlying": symbol,
                    "strategy": "context",
                    "reasons": [reason],
                    "stage": "context",
                }
            )
            continue
        current_symbol_health = _symbol_scan_health(
            context,
            gates=config.get("gates", {}),
            as_of=as_of,
        )
        symbol_health.append(current_symbol_health)
        earnings_research = scan_earnings_calendar_candidates(context, research_cfg)
        vol_crush_research = scan_volatility_crush_iron_condor_candidates(context, vol_crush_cfg)
        research_candidates.extend(earnings_research)
        research_candidates.extend(vol_crush_research)
        research_traces.append(
            _research_trace_entry(
                symbol=str(underlying["symbol"]),
                scanner="earnings_calendar_spread",
                enabled=bool(research_cfg.get("enabled", False)),
                candidates=earnings_research,
            )
        )
        research_traces.append(
            _research_trace_entry(
                symbol=str(underlying["symbol"]),
                scanner="volatility_crush_earnings_iron_condor",
                enabled=bool(vol_crush_cfg.get("enabled", False)),
                candidates=vol_crush_research,
            )
        )
        # Cheap, local structural critique runs per candidate so it can feed
        # into pre-trade gating. Expensive external AI checks are deferred
        # until after global ranking so we only pay for candidates that might
        # actually execute.
        for strategy in strategies:
            order = strategy.generate_order(context)
            if order is None:
                current_symbol_health["strategy_diagnostics"].append(
                    _strategy_generation_diagnostics(strategy, context)
                )
                continue
            critique = critique_trade(order)
            structural_severity = str(critique.get("severity", "none"))
            if structural_severity == "major":
                order.ai_veto_reason = "trade_critic_major_concern"
            structural_concerns = [
                str(item)
                for item in critique.get("concerns", [])
                if isinstance(item, str) and item.strip()
            ]
            decision = pre_trade_check(
                order,
                account=account,
                config=config,
                open_positions=positions,
                as_of=as_of,
            )
            order.metadata["pre_ai_feature_packet"] = build_pre_ai_feature_packet(
                order,
                structural_severity=structural_severity,
                structural_concerns=structural_concerns,
                risk_warnings=decision.warnings,
            )
            if not decision.accepted:
                ai_disagreements.append(
                    build_ai_disagreement_entry(
                        order,
                        stage="deterministic_pre_trade",
                        disagreement_type="deterministic_reject_before_ai",
                        deterministic_decision="reject",
                        ai_decision="not_run",
                        structural_severity=structural_severity,
                        reasons=decision.reasons,
                    )
                )
                rejected.append(
                    {
                        "underlying": underlying["symbol"],
                        "strategy": strategy.name,
                        "reasons": decision.reasons,
                    }
                )
                continue
            candidate_pool.append(
                build_candidate(
                    order,
                    context=context,
                    config=config,
                    structural_severity=structural_severity,
                    warnings=decision.warnings,
                )
            )

    ranked_candidates = rank_candidates(candidate_pool)
    selected_underlyings = {position.underlying for position in positions}
    remaining_slots = _remaining_entry_slots(config, len(positions))
    for candidate in ranked_candidates:
        if remaining_slots <= 0:
            break
        if candidate.underlying in selected_underlyings:
            continue
        decision = pre_trade_check(
            candidate.order,
            account=account,
            config=config,
            open_positions=positions,
            as_of=as_of,
        )
        existing_packet = candidate.order.metadata.get("pre_ai_feature_packet")
        deterministic_review = (
            existing_packet.get("deterministic_review", {})
            if isinstance(existing_packet, dict)
            else {}
        )
        candidate.order.metadata["pre_ai_feature_packet"] = build_pre_ai_feature_packet(
            candidate.order,
            structural_severity=candidate.structural_severity,
            structural_concerns=deterministic_review.get("structural_concerns", []),
            risk_warnings=decision.warnings,
        )
        if not decision.accepted:
            ai_disagreements.append(
                build_ai_disagreement_entry(
                    candidate.order,
                    stage="post_selection_pre_ai",
                    disagreement_type="deterministic_reject_before_ai",
                    deterministic_decision="reject",
                    ai_decision="not_run",
                    structural_severity=candidate.structural_severity,
                    reasons=decision.reasons,
                )
            )
            rejected.append(
                {
                    "underlying": candidate.underlying,
                    "strategy": candidate.strategy_name,
                    "reasons": decision.reasons,
                    "stage": "post_selection",
                }
            )
            continue
        if not candidate.order.ai_veto_reason:
            external = evaluate_external_ai(
                candidate.order,
                config=config,
                structural_severity=candidate.structural_severity,
            )
            external_reason = str(external.get("veto_reason") or "")
            if external_reason:
                # Veto-only: external checks can add a veto reason but
                # never clear an existing structural one.
                candidate.order.ai_veto_reason = external_reason
                ai_disagreements.append(
                    build_ai_disagreement_entry(
                        candidate.order,
                        stage="external_ai",
                        disagreement_type="ai_veto_after_deterministic_accept",
                        deterministic_decision="accept",
                        ai_decision="veto",
                        structural_severity=candidate.structural_severity,
                        reasons=[external_reason],
                        external=external,
                    )
                )
        decision = pre_trade_check(
            candidate.order,
            account=account,
            config=config,
            open_positions=positions,
            as_of=as_of,
        )
        if not decision.accepted:
            rejected.append(
                {
                    "underlying": candidate.underlying,
                    "strategy": candidate.strategy_name,
                    "reasons": decision.reasons,
                    "stage": "external_ai",
                }
            )
            continue
        result = execute_order(client, candidate.order, dry_run=dry_run, config=config)
        accepted.append(
            {
                "underlying": candidate.underlying,
                "strategy": candidate.strategy_name,
                "score": round(float(candidate.score), 6),
                "selection": dict(candidate.order.metadata.get("selection", {})),
                "pre_ai_feature_packet": dict(candidate.order.metadata.get("pre_ai_feature_packet", {})),
                "order": result,
            }
        )
        if not dry_run:
            position = persist_open_order(
                order=candidate.order,
                mode=str(config.get("mode", "paper")),
                order_id=str(result["id"]),
                trade_log_path=paths["trade_log"],
                positions_path=paths["positions"],
                execution_result=result,
            )
        else:
            position = position_from_order(candidate.order)
        positions.append(position)
        selected_underlyings.add(candidate.underlying)
        remaining_slots -= 1
    result = {
        "as_of": as_of.isoformat(),
        "dry_run": dry_run,
        "candidate_count": len(ranked_candidates),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "ranked_candidates": [candidate.summary() for candidate in ranked_candidates],
        "research_candidates": research_candidates,
        "research_traces": research_traces,
        "ai_disagreements": ai_disagreements,
        "accepted": accepted,
        "rejected": rejected,
    }
    result["scan_health"] = _scan_health_summary(
        symbol_health=symbol_health,
        ranked_candidates=result["ranked_candidates"],
        accepted=accepted,
        rejected=rejected,
    )
    report_path = _persist_candidate_scan(
        paths=paths,
        payload=result,
        config=config,
        account=account,
        underlyings=underlyings,
        open_position_count=starting_open_position_count,
    )
    result["candidate_report_path"] = str(report_path)
    rejection_report_path = _persist_rejection_report(paths=paths, payload=result)
    result["rejection_report_path"] = str(rejection_report_path)
    scan_health_report_path = _persist_scan_health_report(paths=paths, payload=result)
    result["scan_health_report_path"] = str(scan_health_report_path)
    research_trace_path = _persist_research_trace(paths=paths, payload=result)
    result["research_trace_path"] = str(research_trace_path)
    ai_disagreement_path = _persist_ai_disagreement_log(paths=paths, payload=result)
    result["ai_disagreement_path"] = str(ai_disagreement_path)
    return result


def _market_context_for_scan(*, config: dict[str, Any], client: Any, as_of: date) -> dict[str, Any]:
    if not _vix_scaling_enabled(config):
        return {}
    market_context_getter = getattr(client, "get_market_volatility_snapshot", None)
    if not callable(market_context_getter):
        return {}
    try:
        snapshot = market_context_getter(as_of=as_of)
    except Exception as exc:
        _LOGGER.warning("market volatility snapshot unavailable: %s", exc)
        return {
            "vix": None,
            "source": "market_volatility_snapshot_error",
            "reason": str(exc),
            "as_of": as_of.isoformat(),
        }
    return snapshot if isinstance(snapshot, dict) else {}


def _vix_scaling_enabled(config: dict[str, Any]) -> bool:
    gates = config.get("gates") if isinstance(config, dict) else {}
    if not isinstance(gates, dict):
        return False
    scaling = gates.get("vix_iv_rank_scaling", {})
    return isinstance(scaling, dict) and bool(scaling.get("enabled", False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the HawksOptions market scan")
    parser.add_argument("--dry-run", action="store_true", help="Generate orders but do not persist them")
    args = parser.parse_args(argv)
    result = scan_market(config={}, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
