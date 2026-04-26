"""Shared strategy type groupings."""

from __future__ import annotations


SHORT_PREMIUM_STRATEGIES = {
    "cash_secured_put",
    "covered_call",
    "vertical_spread",
    "iron_condor",
    "earnings_iron_condor",
}

LONG_PREMIUM_STRATEGIES = {"calendar_spread"}
