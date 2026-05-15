"""Read-only scanner for earnings calendar-spread research candidates."""

from __future__ import annotations

import math
from datetime import date, timedelta
from typing import Any

from core.contract_selector import select_iron_condor
from core.models import OptionContract, StrategyContext
from core.options_chain import filter_contracts


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def scan_earnings_calendar_candidates(
    context: StrategyContext,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    if not bool(params.get("enabled", False)):
        return []
    allowed = set(context.underlying.get("strategies_allowed", []))
    if "calendar_spread" not in allowed and "earnings_calendar_spread" not in allowed:
        return []
    earnings_date = _as_date(context.next_earnings_date or context.underlying.get("next_earnings_date"))
    if earnings_date is None:
        return []
    days_to_earnings = (earnings_date - context.as_of).days
    if days_to_earnings < int(params.get("min_days_to_earnings", 1)):
        return []
    if days_to_earnings > int(params.get("max_days_to_earnings", 10)):
        return []
    confidence = float(context.underlying.get("earnings_date_confidence", 1.0))
    if confidence < float(params.get("min_earnings_confidence", 0.8)):
        return []
    if context.iv_rank < float(params.get("min_iv_rank", 50.0)):
        return []

    planned_exit = earnings_date + timedelta(days=int(params.get("exit_days_after_earnings", 1)))
    candidates: list[dict[str, Any]] = []
    for option_type in _option_types(params):
        contracts = _filtered_contracts(context, params, option_type)
        front_candidates = [
            contract
            for contract in contracts
            if contract.expiration > earnings_date
            and contract.days_to_expiration(context.as_of) <= int(params.get("front_max_dte", 21))
        ]
        for front in front_candidates:
            if planned_exit >= front.expiration:
                continue
            if _ex_dividend_too_close(context, front):
                continue
            if _assignment_risk(front, float(params.get("max_assignment_intrinsic_slippage", 0.05))):
                continue
            for back in _matching_back_legs(contracts, front, params, context):
                iv_spread = float(front.implied_volatility or 0.0) - float(back.implied_volatility or 0.0)
                if iv_spread < float(params.get("min_front_back_iv_spread", 0.03)):
                    continue
                debit = round((back.mid_price() - front.mid_price()) * 100.0, 2)
                if debit <= 0 or debit > float(params.get("max_debit", 250.0)):
                    continue
                candidates.append(
                    {
                        "underlying": context.underlying["symbol"],
                        "strategy": "earnings_calendar_spread",
                        "option_type": option_type,
                        "front_contract": front.contract_symbol,
                        "back_contract": back.contract_symbol,
                        "strike": front.strike,
                        "front_expiration": front.expiration.isoformat(),
                        "back_expiration": back.expiration.isoformat(),
                        "earnings_date": earnings_date.isoformat(),
                        "planned_exit_date": planned_exit.isoformat(),
                        "earnings_confidence": confidence,
                        "iv_rank": context.iv_rank,
                        "front_back_iv_spread": round(iv_spread, 4),
                        "debit": debit,
                        "score": _score(front, back, iv_spread, debit),
                    }
                )
    candidates.sort(key=lambda item: (-float(item["score"]), float(item["debit"])))
    return candidates[: int(params.get("max_candidates_per_underlying", 5))]


def scan_volatility_crush_iron_condor_candidates(
    context: StrategyContext,
    params: dict[str, Any],
) -> list[dict[str, Any]]:
    if not bool(params.get("enabled", False)):
        return []
    allowed = set(context.underlying.get("strategies_allowed", []))
    if "iron_condor" not in allowed and "earnings_iron_condor" not in allowed:
        return []
    earnings_date = _as_date(context.next_earnings_date or context.underlying.get("next_earnings_date"))
    if earnings_date is None:
        return []
    days_to_earnings = (earnings_date - context.as_of).days
    if days_to_earnings < int(params.get("min_days_to_earnings", 0)):
        return []
    if days_to_earnings > int(params.get("max_days_to_earnings", 3)):
        return []
    if context.iv_rank < float(params.get("min_iv_rank", 70.0)):
        return []
    planned_exit = earnings_date + timedelta(days=int(params.get("exit_days_after_earnings", 1)))
    contracts = _filtered_condor_contracts(context, params)
    selected = select_iron_condor(
        contracts,
        put_short_delta=float(params.get("put_short_delta", -0.16)),
        put_long_delta=float(params.get("put_long_delta", -0.08)),
        call_short_delta=float(params.get("call_short_delta", 0.16)),
        call_long_delta=float(params.get("call_long_delta", 0.08)),
        target_dte=int(params.get("target_dte", 14)),
        as_of=context.as_of,
    )
    if selected is None:
        return []
    short_put, long_put, short_call, long_call = selected
    expiration = short_put.expiration
    if len({expiration, long_put.expiration, short_call.expiration, long_call.expiration}) != 1:
        return []
    if planned_exit >= expiration:
        return []
    if _condor_ex_dividend_too_close(context, expiration):
        return []
    expected_move = _expected_move(context, expiration)
    if bool(params.get("require_short_strikes_outside_expected_move", True)):
        lower_bound = context.underlying_price - expected_move
        upper_bound = context.underlying_price + expected_move
        if short_put.strike > lower_bound or short_call.strike < upper_bound:
            return []
    credit = round((short_put.mid_price() + short_call.mid_price() - long_put.mid_price() - long_call.mid_price()) * 100.0, 2)
    put_width = abs(short_put.strike - long_put.strike) * 100.0
    call_width = abs(long_call.strike - short_call.strike) * 100.0
    max_loss = round(max(put_width, call_width) - credit, 2)
    if credit < float(params.get("min_credit", 50.0)) or max_loss <= 0:
        return []
    if max_loss > float(params.get("max_loss", 500.0)):
        return []
    equity = float(context.account.get("equity") or context.account.get("portfolio_value") or 0.0)
    if equity > 0 and max_loss > equity * float(params.get("max_event_risk_pct", 0.01)):
        return []
    return [
        {
            "underlying": context.underlying["symbol"],
            "strategy": "volatility_crush_earnings_iron_condor",
            "expiration": expiration.isoformat(),
            "earnings_date": earnings_date.isoformat(),
            "planned_exit_date": planned_exit.isoformat(),
            "expected_move": round(expected_move, 4),
            "short_put": short_put.contract_symbol,
            "long_put": long_put.contract_symbol,
            "short_call": short_call.contract_symbol,
            "long_call": long_call.contract_symbol,
            "short_put_strike": short_put.strike,
            "short_call_strike": short_call.strike,
            "credit": credit,
            "max_loss": max_loss,
            "iv_rank": context.iv_rank,
            "score": round((credit / max_loss) + (context.iv_rank / 100.0), 6),
        }
    ]


def _option_types(params: dict[str, Any]) -> list[str]:
    configured = params.get("option_types", ["call", "put"])
    return [item for item in configured if item in {"call", "put"}]


def _filtered_contracts(
    context: StrategyContext,
    params: dict[str, Any],
    option_type: str,
) -> list[OptionContract]:
    gates = context.config.get("gates", {})
    return filter_contracts(
        context.chain,
        min_open_interest=int(params.get("min_open_interest", gates.get("min_open_interest", 100))),
        min_daily_volume=int(params.get("min_daily_volume", gates.get("min_daily_volume", 10))),
        max_bid_ask_spread_pct=float(params.get("max_bid_ask_spread_pct", gates.get("max_bid_ask_spread_pct", 0.10))),
        min_dte=int(params.get("front_min_dte", gates.get("min_dte_entry", 7))),
        max_dte=int(params.get("back_max_dte", gates.get("max_dte_entry", 55))),
        as_of=context.as_of,
        option_type=option_type,
    )


def _filtered_condor_contracts(context: StrategyContext, params: dict[str, Any]) -> list[OptionContract]:
    gates = context.config.get("gates", {})
    return filter_contracts(
        context.chain,
        min_open_interest=int(params.get("min_open_interest", gates.get("min_open_interest", 100))),
        min_daily_volume=int(params.get("min_daily_volume", gates.get("min_daily_volume", 10))),
        max_bid_ask_spread_pct=float(params.get("max_bid_ask_spread_pct", gates.get("max_bid_ask_spread_pct", 0.10))),
        min_dte=int(params.get("dte_min", gates.get("min_dte_entry", 7))),
        max_dte=int(params.get("dte_max", gates.get("max_dte_entry", 55))),
        as_of=context.as_of,
    )


def _matching_back_legs(
    contracts: list[OptionContract],
    front: OptionContract,
    params: dict[str, Any],
    context: StrategyContext,
) -> list[OptionContract]:
    back_target_dte = int(params.get("back_target_dte", 45))
    return sorted(
        [
            contract
            for contract in contracts
            if contract.strike == front.strike
            and contract.expiration > front.expiration
            and contract.days_to_expiration(context.as_of) <= int(params.get("back_max_dte", 55))
        ],
        key=lambda contract: (
            abs(contract.days_to_expiration(context.as_of) - back_target_dte),
            contract.spread_pct(),
        ),
    )


def _ex_dividend_too_close(context: StrategyContext, front: OptionContract) -> bool:
    ex_dividend_date = _as_date(context.ex_dividend_date or context.underlying.get("ex_dividend_date"))
    if ex_dividend_date is None:
        return False
    return context.as_of <= ex_dividend_date <= front.expiration


def _condor_ex_dividend_too_close(context: StrategyContext, expiration: date) -> bool:
    ex_dividend_date = _as_date(context.ex_dividend_date or context.underlying.get("ex_dividend_date"))
    if ex_dividend_date is None:
        return False
    return context.as_of <= ex_dividend_date <= expiration


def _assignment_risk(front: OptionContract, slippage: float) -> bool:
    if not front.is_itm():
        return False
    intrinsic = (
        max(0.0, float(front.underlying_price) - float(front.strike))
        if front.option_type == "call"
        else max(0.0, float(front.strike) - float(front.underlying_price))
    )
    return intrinsic > 0 and float(front.mid_price()) <= intrinsic + max(0.0, slippage)


def _score(front: OptionContract, back: OptionContract, iv_spread: float, debit: float) -> float:
    liquidity = min(front.open_interest, back.open_interest) + min(front.volume, back.volume)
    return round((iv_spread * 100.0) + (liquidity / 1000.0) - (debit / 1000.0), 6)


def _expected_move(context: StrategyContext, expiration: date) -> float:
    dte = max(1, (expiration - context.as_of).days)
    iv = float(context.current_iv or context.iv_rank / 100.0)
    return float(context.underlying_price) * iv * math.sqrt(dte / 365.0)
