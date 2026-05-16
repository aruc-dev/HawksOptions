"""Lightweight volatility-surface analytics for strategy filters."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from core.models import OptionContract


def volatility_surface_metrics(
    chain: list[OptionContract],
    *,
    underlying_price: float,
    as_of: date,
    front_dte: int = 30,
    back_dte: int = 45,
) -> dict[str, float | None]:
    return {
        "put_tail_skew": tail_skew(chain, option_type="put", underlying_price=underlying_price),
        "call_tail_skew": tail_skew(chain, option_type="call", underlying_price=underlying_price),
        "term_structure_slope": term_structure_slope(
            chain,
            underlying_price=underlying_price,
            as_of=as_of,
            front_dte=front_dte,
            back_dte=back_dte,
        ),
    }


def tail_skew(
    chain: list[OptionContract],
    *,
    option_type: str,
    underlying_price: float,
) -> float | None:
    atm_iv = _atm_iv(chain, underlying_price=underlying_price)
    if atm_iv is None:
        return None
    if option_type == "put":
        candidates = [
            contract
            for contract in chain
            if contract.option_type == "put"
            and contract.strike < underlying_price
            and contract.implied_volatility > 0
        ]
    elif option_type == "call":
        candidates = [
            contract
            for contract in chain
            if contract.option_type == "call"
            and contract.strike > underlying_price
            and contract.implied_volatility > 0
        ]
    else:
        return None
    if not candidates:
        return None
    tail = sorted(
        candidates,
        key=lambda contract: (
            abs(abs(contract.delta or 0.0) - 0.20),
            abs(contract.strike - underlying_price),
        ),
    )[0]
    return round(float(tail.implied_volatility) - atm_iv, 6)


def term_structure_slope(
    chain: list[OptionContract],
    *,
    underlying_price: float,
    as_of: date,
    front_dte: int = 30,
    back_dte: int = 45,
) -> float | None:
    by_expiration: dict[date, list[OptionContract]] = defaultdict(list)
    for contract in chain:
        if contract.implied_volatility > 0:
            by_expiration[contract.expiration].append(contract)
    if len(by_expiration) < 2:
        return None
    expirations = sorted(by_expiration)
    front_expiration = min(expirations, key=lambda expiration: abs((expiration - as_of).days - front_dte))
    back_candidates = [expiration for expiration in expirations if expiration > front_expiration]
    if not back_candidates:
        return None
    back_expiration = min(back_candidates, key=lambda expiration: abs((expiration - as_of).days - back_dte))
    front_iv = _atm_iv(by_expiration[front_expiration], underlying_price=underlying_price)
    back_iv = _atm_iv(by_expiration[back_expiration], underlying_price=underlying_price)
    if front_iv is None or back_iv is None:
        return None
    return round(back_iv - front_iv, 6)


def _atm_iv(chain: list[OptionContract], *, underlying_price: float) -> float | None:
    candidates = [contract for contract in chain if contract.implied_volatility > 0]
    if not candidates:
        return None
    nearest = sorted(candidates, key=lambda contract: (abs(contract.strike - underlying_price), contract.spread_pct()))
    strike = nearest[0].strike
    same_strike = [contract for contract in candidates if contract.strike == strike]
    return round(sum(float(contract.implied_volatility) for contract in same_strike) / len(same_strike), 6)
