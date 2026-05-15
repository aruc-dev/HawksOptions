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


def max_pain_strike(chain: list[OptionContract]) -> float | None:
    profile = open_interest_profile(chain)
    if not profile or sum(bucket["total"] for bucket in profile.values()) <= 0:
        return None
    payouts: dict[float, float] = {}
    for settlement in profile:
        total_payout = 0.0
        for contract in chain:
            oi = max(0, int(contract.open_interest))
            if contract.option_type == "call":
                total_payout += max(0.0, settlement - float(contract.strike)) * oi
            elif contract.option_type == "put":
                total_payout += max(0.0, float(contract.strike) - settlement) * oi
        payouts[settlement] = total_payout
    return min(payouts, key=lambda strike: (payouts[strike], abs(strike)))


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
    max_pain = max_pain_strike(chain)
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
