"""Assignment-awareness logic for short option positions."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Iterable

from core.models import OrderLeg, PositionSnapshot


def _itm_short_call_legs(legs: Iterable[OrderLeg]) -> list[OrderLeg]:
    return [
        leg
        for leg in legs
        if leg.side == "sell_to_open"
        and leg.contract.option_type == "call"
        and leg.contract.is_itm()
    ]


def should_close_short_call_for_ex_div(
    position: PositionSnapshot,
    *,
    as_of: date | None = None,
) -> bool:
    """Return True if a short *call* leg should be closed before
    ex-dividend.

    Only short calls carry early-exercise risk around ex-dividend
    dates; short puts never benefit a holder from early exercise on
    a dividend payment, so we short-circuit there.
    """
    as_of = as_of or date.today()
    if position.ex_dividend_date is None:
        return False
    if position.ex_dividend_date > (as_of + timedelta(days=1)):
        return False
    # Only ITM short calls are at risk of early exercise for the dividend.
    if not _itm_short_call_legs(position.legs):
        return False
    if position.dividend_amount <= 0:
        return False
    return position.dividend_amount > position.remaining_extrinsic_value


def calendar_front_assignment_risk(
    position: PositionSnapshot,
    *,
    slippage: float = 0.05,
) -> bool:
    """Return True when a calendar/diagonal front short leg looks vulnerable
    to early assignment.

    Calendar and debit diagonal spreads are only defined-risk if held
    through front expiration. If the ITM short front leg's mid trades at
    or below its intrinsic value (within a small slippage band), holders
    of the long side have an incentive to exercise. We treat that
    condition as a flag-for-close signal.

    The slippage band defaults to $0.05 per share (= $5 per contract);
    callers can pass a config-driven value.
    """
    if position.strategy_name not in {"calendar_spread", "diagonal_spread"}:
        return False
    short_legs = [leg for leg in position.legs if leg.side == "sell_to_open"]
    long_legs = [leg for leg in position.legs if leg.side == "buy_to_open"]
    if not short_legs or not long_legs:
        return False
    # The front leg is the short with the nearest expiration.
    front = min(short_legs, key=lambda leg: leg.contract.expiration)
    contract = front.contract
    intrinsic = (
        max(0.0, float(contract.underlying_price) - float(contract.strike))
        if contract.option_type == "call"
        else max(0.0, float(contract.strike) - float(contract.underlying_price))
    )
    if not contract.is_itm() or intrinsic <= 0.0:
        return False
    mid = float(contract.mid_price())
    if mid <= 0.0:
        return False
    # If the option trades at intrinsic or below (within slippage), an
    # arbitrageur would exercise; we want out before that happens.
    return mid <= intrinsic + max(0.0, float(slippage))


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
