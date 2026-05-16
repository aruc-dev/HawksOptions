"""Open-interest profile and max-pain analytics."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from core.models import OptionContract


def open_interest_profile(chain: list[OptionContract]) -> dict[float, dict[str, int]]:
    profile: dict[float, dict[str, int]] = defaultdict(lambda: {"call": 0, "put": 0, "total": 0})
    for contract in chain:
        oi = max(0, int(contract.open_interest))
        bucket = profile[float(contract.strike)]
        if contract.option_type in {"call", "put"}:
            bucket[contract.option_type] += oi
        bucket["total"] += oi
    return dict(sorted(profile.items()))


def max_pain_strike(chain: list[OptionContract], *, underlying_price: float | None = None) -> float | None:
    profile = open_interest_profile(chain)
    if not profile or sum(bucket["total"] for bucket in profile.values()) <= 0:
        return None
    strikes = list(profile)
    call_prefix_count: list[int] = []
    call_prefix_strike_oi: list[float] = []
    put_suffix_count: list[int] = [0] * len(strikes)
    put_suffix_strike_oi: list[float] = [0.0] * len(strikes)

    running_count = 0
    running_strike_oi = 0.0
    for strike in strikes:
        running_count += profile[strike]["call"]
        running_strike_oi += strike * profile[strike]["call"]
        call_prefix_count.append(running_count)
        call_prefix_strike_oi.append(running_strike_oi)

    running_count = 0
    running_strike_oi = 0.0
    for index in range(len(strikes) - 1, -1, -1):
        strike = strikes[index]
        running_count += profile[strike]["put"]
        running_strike_oi += strike * profile[strike]["put"]
        put_suffix_count[index] = running_count
        put_suffix_strike_oi[index] = running_strike_oi

    payouts: dict[float, float] = {}
    for index, settlement in enumerate(strikes):
        call_payout = 0.0
        if index > 0:
            call_payout = (settlement * call_prefix_count[index - 1]) - call_prefix_strike_oi[index - 1]
        put_payout = 0.0
        if index < len(strikes) - 1:
            put_payout = put_suffix_strike_oi[index + 1] - (settlement * put_suffix_count[index + 1])
        payouts[settlement] = call_payout + put_payout
    reference = underlying_price if underlying_price is not None and underlying_price > 0 else strikes[len(strikes) // 2]
    return min(payouts, key=lambda strike: (payouts[strike], abs(strike - reference), strike))


def open_interest_context(chain: list[OptionContract], *, underlying_price: float) -> dict[str, Any]:
    profile = open_interest_profile(chain)
    total_oi = sum(bucket["total"] for bucket in profile.values())
    if not profile or total_oi <= 0:
        return {
            "max_pain_strike": None,
            "max_pain_distance_pct": None,
            "largest_oi_strike": None,
            "largest_oi": 0,
            "total_open_interest": 0,
        }
    max_pain = max_pain_strike(chain, underlying_price=underlying_price)
    largest_strike, largest_bucket = max(profile.items(), key=lambda item: (item[1]["total"], -abs(item[0] - underlying_price)))
    distance_pct = None
    if max_pain is not None and underlying_price > 0:
        distance_pct = round((float(max_pain) - underlying_price) / underlying_price, 6)
    return {
        "max_pain_strike": max_pain,
        "max_pain_distance_pct": distance_pct,
        "largest_oi_strike": largest_strike,
        "largest_oi": largest_bucket["total"],
        "total_open_interest": total_oi,
    }
