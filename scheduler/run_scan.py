"""Run a scan across the configured watchlist."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import date
from typing import Any
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from ai.news_client import fetch_headlines
from ai.news_gate import evaluate_news_gate
from ai.openai_client import critique_with_llm
from ai.trade_idea_critic import critique_trade
from core.models import StrategyOrder
from core.order_executor import execute_order, persist_open_order, position_from_order
from core.risk_manager import pre_trade_check
from scheduler.common import build_context, configured_underlyings, current_positions, load_runtime
from strategies import build_enabled_strategies
from strategies.selection import build_candidate, rank_candidates

_LOGGER = logging.getLogger(__name__)


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
    return {
        "strategy_name": order.strategy_name,
        "underlying": order.underlying,
        "max_loss": round(float(order.max_loss), 2),
        "max_profit": round(float(order.max_profit), 2),
        "net_opening_credit": float(order.net_opening_credit),
        "iv_rank": round(float(order.iv_rank), 2),
        "min_dte": int(order.min_dte),
        "next_earnings_date": (
            order.next_earnings_date.isoformat() if order.next_earnings_date else None
        ),
        "ex_dividend_date": (
            order.ex_dividend_date.isoformat() if order.ex_dividend_date else None
        ),
        "legs": [
            {
                "side": leg.side,
                "qty": int(leg.qty),
                "option_type": leg.contract.option_type,
                "strike": float(leg.contract.strike),
                "expiration": leg.contract.expiration.isoformat(),
                "delta": leg.contract.delta,
            }
            for leg in order.legs
        ],
    }


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

    try:
        llm_result = llm(
            _order_summary(order),
            headlines=[h for h in (result.get("news") or {}).get("matched_headlines", [])]
            if isinstance(result.get("news"), dict)
            else [],
            daily_spend_cap_usd=float(ai_cfg.get("daily_spend_cap_usd", 5.0)),
        )
    except Exception as exc:  # pragma: no cover — client is bounded
        _LOGGER.warning("openai critic failed for %s: %s", order.underlying, exc)
        return result

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
    positions = current_positions(paths)
    strategies = build_enabled_strategies(config)
    candidate_pool = []
    accepted = []
    rejected = []
    for underlying in configured_underlyings(config):
        context = build_context(
            config=config,
            client=client,
            underlying=underlying,
            account=account,
            open_positions=positions,
            as_of=as_of,
        )
        # Cheap, local structural critique runs per candidate so it can feed
        # into pre-trade gating. Expensive external AI checks are deferred
        # until after global ranking so we only pay for candidates that might
        # actually execute.
        for strategy in strategies:
            order = strategy.generate_order(context)
            if order is None:
                continue
            critique = critique_trade(order)
            structural_severity = str(critique.get("severity", "none"))
            if structural_severity == "major":
                order.ai_veto_reason = "trade_critic_major_concern"
            decision = pre_trade_check(
                order,
                account=account,
                config=config,
                open_positions=positions,
                as_of=as_of,
            )
            if not decision.accepted:
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
        if not decision.accepted:
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
        result = execute_order(client, candidate.order, dry_run=dry_run)
        accepted.append(
            {
                "underlying": candidate.underlying,
                "strategy": candidate.strategy_name,
                "score": round(float(candidate.score), 6),
                "selection": dict(candidate.order.metadata.get("selection", {})),
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
            )
        else:
            position = position_from_order(candidate.order)
        positions.append(position)
        selected_underlyings.add(candidate.underlying)
        remaining_slots -= 1
    return {
        "as_of": as_of.isoformat(),
        "candidate_count": len(ranked_candidates),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "ranked_candidates": [candidate.summary() for candidate in ranked_candidates],
        "accepted": accepted,
        "rejected": rejected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the HawksOptions market scan")
    parser.add_argument("--dry-run", action="store_true", help="Generate orders but do not persist them")
    args = parser.parse_args(argv)
    result = scan_market(config={}, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
