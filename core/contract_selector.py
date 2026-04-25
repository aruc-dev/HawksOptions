"""Strike and expiry selection helpers."""

from __future__ import annotations

from datetime import date, datetime
from typing import Iterable

from core.models import OptionContract


def _sort_key(
    contract: OptionContract,
    target_delta: float,
    target_dte: int | None,
    as_of: date | datetime | None,
) -> tuple[float, int, float]:
    delta_gap = abs(abs(contract.delta or 0.0) - abs(target_delta))
    dte_gap = abs(contract.days_to_expiration(as_of) - target_dte) if target_dte is not None else 0
    return (delta_gap, dte_gap, contract.spread_pct())


def find_by_delta(
    chain: Iterable[OptionContract],
    target_delta: float,
    tolerance: float = 0.05,
    target_dte: int | None = None,
    as_of: date | datetime | None = None,
) -> OptionContract | None:
    candidates = []
    for contract in chain:
        if contract.delta is None:
            continue
        if abs(abs(contract.delta) - abs(target_delta)) <= tolerance:
            candidates.append(contract)
    if not candidates:
        return None
    candidates.sort(key=lambda contract: _sort_key(contract, target_delta, target_dte, as_of))
    return candidates[0]


def select_vertical_spread(
    chain: Iterable[OptionContract],
    *,
    short_delta: float,
    long_delta: float,
    option_type: str,
    target_dte: int | None = None,
    as_of: date | datetime | None = None,
) -> tuple[OptionContract, OptionContract] | None:
    typed = [contract for contract in chain if contract.option_type == option_type]
    short_leg = find_by_delta(typed, short_delta, target_dte=target_dte, as_of=as_of)
    if short_leg is None:
        return None
    if option_type == "put":
        long_candidates = [
            contract
            for contract in typed
            if contract.expiration == short_leg.expiration
            and contract.delta is not None
            and contract.strike < short_leg.strike
        ]
        long_candidates.sort(
            key=lambda contract: (
                abs(short_leg.strike - contract.strike),
                abs(abs(contract.delta or 0.0) - abs(long_delta)),
                contract.spread_pct(),
            )
        )
    else:
        long_candidates = [
            contract
            for contract in typed
            if contract.expiration == short_leg.expiration
            and contract.delta is not None
            and contract.strike > short_leg.strike
        ]
        long_candidates.sort(
            key=lambda contract: (
                abs(contract.strike - short_leg.strike),
                abs(abs(contract.delta or 0.0) - abs(long_delta)),
                contract.spread_pct(),
            )
        )
    if not long_candidates:
        return None
    return short_leg, long_candidates[0]


def select_iron_condor(
    chain: Iterable[OptionContract],
    *,
    put_short_delta: float,
    put_long_delta: float,
    call_short_delta: float,
    call_long_delta: float,
    target_dte: int | None = None,
    as_of: date | datetime | None = None,
) -> tuple[OptionContract, OptionContract, OptionContract, OptionContract] | None:
    puts = [contract for contract in chain if contract.option_type == "put"]
    calls = [contract for contract in chain if contract.option_type == "call"]
    put_pair = select_vertical_spread(
        puts,
        short_delta=put_short_delta,
        long_delta=put_long_delta,
        option_type="put",
        target_dte=target_dte,
        as_of=as_of,
    )
    call_pair = select_vertical_spread(
        calls,
        short_delta=call_short_delta,
        long_delta=call_long_delta,
        option_type="call",
        target_dte=target_dte,
        as_of=as_of,
    )
    if put_pair is None or call_pair is None:
        return None
    short_put, long_put = put_pair
    short_call, long_call = call_pair
    if len({short_put.expiration, long_put.expiration, short_call.expiration, long_call.expiration}) != 1:
        return None
    return short_put, long_put, short_call, long_call
