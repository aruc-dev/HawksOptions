"""Assignment-awareness logic for short option positions."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from core.models import PositionSnapshot


def should_close_short_call_for_ex_div(
    position: PositionSnapshot,
    *,
    as_of: date | None = None,
) -> bool:
    as_of = as_of or date.today()
    if position.ex_dividend_date is None:
        return False
    if position.ex_dividend_date > (as_of + timedelta(days=1)):
        return False
    if not position.short_leg_itm:
        return False
    if position.dividend_amount <= 0:
        return False
    return position.dividend_amount > position.remaining_extrinsic_value


def detect_assignments(
    previous_positions: Iterable[PositionSnapshot],
    current_symbols: Iterable[str],
) -> list[dict[str, str]]:
    symbols = {str(symbol).upper() for symbol in current_symbols}
    events: list[dict[str, str]] = []
    for position in previous_positions:
        short_legs = [leg for leg in position.legs if leg.side == "sell_to_open"]
        if not short_legs:
            continue
        underlying_present = position.underlying.upper() in symbols
        options_remaining = any(leg.contract.contract_symbol.upper() in symbols for leg in short_legs)
        if underlying_present and not options_remaining:
            events.append(
                {
                    "strategy_id": position.strategy_id,
                    "underlying": position.underlying,
                    "strategy_name": position.strategy_name,
                    "event": "assignment_detected",
                }
            )
    return events
