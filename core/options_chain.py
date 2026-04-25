"""Options-chain filtering helpers."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from typing import Iterable

from core.models import OptionContract


def _to_date(value: date | datetime | None) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.today()


def passes_liquidity_gates(
    contract: OptionContract,
    *,
    min_open_interest: int,
    min_daily_volume: int,
    max_bid_ask_spread_pct: float,
) -> bool:
    if contract.open_interest < min_open_interest:
        return False
    if contract.volume < min_daily_volume:
        return False
    if contract.spread_pct() > max_bid_ask_spread_pct:
        return False
    return contract.mid_price() > 0.0


def filter_contracts(
    chain: Iterable[OptionContract],
    *,
    min_open_interest: int,
    min_daily_volume: int,
    max_bid_ask_spread_pct: float,
    min_dte: int,
    max_dte: int,
    as_of: date | datetime | None = None,
    option_type: str | None = None,
) -> list[OptionContract]:
    ref = _to_date(as_of)
    out = []
    for contract in chain:
        dte = contract.days_to_expiration(ref)
        if dte < min_dte or dte > max_dte:
            continue
        if option_type and contract.option_type != option_type:
            continue
        if not passes_liquidity_gates(
            contract,
            min_open_interest=min_open_interest,
            min_daily_volume=min_daily_volume,
            max_bid_ask_spread_pct=max_bid_ask_spread_pct,
        ):
            continue
        out.append(contract)
    return out


def group_by_expiration(chain: Iterable[OptionContract]) -> dict[date, list[OptionContract]]:
    grouped: dict[date, list[OptionContract]] = defaultdict(list)
    for contract in chain:
        grouped[contract.expiration].append(contract)
    return dict(grouped)


def split_calls_and_puts(chain: Iterable[OptionContract]) -> tuple[list[OptionContract], list[OptionContract]]:
    calls = [contract for contract in chain if contract.option_type == "call"]
    puts = [contract for contract in chain if contract.option_type == "put"]
    return calls, puts
