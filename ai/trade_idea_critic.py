"""Deterministic structural-trade critic.

Veto-only: the critic returns ``severity == "major"`` to block a trade
or ``"minor"`` for a soft warning. It never originates trades, never
upsizes, and never relaxes risk parameters.
"""

from __future__ import annotations

from core.models import StrategyOrder


SHORT_PREMIUM_STRATEGIES = {
    "cash_secured_put",
    "covered_call",
    "vertical_spread",
    "iron_condor",
    "earnings_iron_condor",
}

LONG_PREMIUM_STRATEGIES = {"calendar_spread"}


def _net_theta(order: StrategyOrder) -> float:
    total = 0.0
    for leg in order.legs:
        sign = -1.0 if leg.side == "sell_to_open" else 1.0
        total += sign * float(leg.contract.theta or 0.0) * leg.qty
    return total


def _net_vega(order: StrategyOrder) -> float:
    total = 0.0
    for leg in order.legs:
        sign = -1.0 if leg.side == "sell_to_open" else 1.0
        total += sign * float(leg.contract.vega or 0.0) * leg.qty
    return total


def _delta_spot_sanity(order: StrategyOrder) -> list[str]:
    """Reject obvious mispriced shorts.

    A short call below spot or a short put above spot is deep ITM and
    has very little extrinsic; nearly always wrong as an entry. We
    allow a small tolerance because the strategy itself may target
    specific deltas.
    """
    concerns: list[str] = []
    for leg in order.legs:
        if leg.side != "sell_to_open":
            continue
        contract = leg.contract
        spot = float(contract.underlying_price)
        if spot <= 0:
            continue
        strike = float(contract.strike)
        if contract.option_type == "call" and strike <= spot * 0.95:
            concerns.append(
                f"short call strike {strike:.2f} more than 5% below spot {spot:.2f}; deep ITM entry"
            )
        if contract.option_type == "put" and strike >= spot * 1.05:
            concerns.append(
                f"short put strike {strike:.2f} more than 5% above spot {spot:.2f}; deep ITM entry"
            )
    return concerns


def critique_trade(order: StrategyOrder) -> dict[str, object]:
    """Return ``{concerns, severity}`` for the given order.

    Severity is ``"major"`` (veto) for any structural problem that
    would directly violate the trading philosophy, ``"minor"`` for
    soft warnings, or ``"none"`` if the order looks healthy.
    """
    major: list[str] = []
    minor: list[str] = []

    # ---- structural sanity checks (always major) -----------------------
    if not order.legs:
        major.append("order has no legs")
    if order.max_loss <= 0:
        major.append("max_loss is not positive")

    # Premium-strategy alignment: short premium needs credit, long
    # premium needs debit. ``net_opening_credit`` is positive for
    # credit trades, negative for debit trades.
    credit = order.net_opening_credit
    if order.strategy_name in SHORT_PREMIUM_STRATEGIES and credit <= 0:
        major.append(
            f"{order.strategy_name} should open for credit but net is {credit:.2f}"
        )
    if order.strategy_name in LONG_PREMIUM_STRATEGIES and credit >= 0:
        major.append(
            f"{order.strategy_name} should open for debit but net is {credit:.2f}"
        )

    # Net theta sanity: short-premium positions should have positive
    # net theta (we are paid to wait). Long-premium positions are
    # bought for a directional/vega reason; calendar spreads in
    # particular are typically theta-positive near front expiration
    # and theta-negative deeper out, so we don't enforce a sign on
    # long premium here. Vega alignment (below) is the sharper signal
    # for long-premium structures.
    theta = _net_theta(order)
    if order.strategy_name in SHORT_PREMIUM_STRATEGIES and theta < 0:
        major.append(
            f"{order.strategy_name} has negative net theta ({theta:.2f}); short premium must collect time decay"
        )

    # Delta-vs-spot sanity (always major).
    major.extend(_delta_spot_sanity(order))

    # ---- soft warnings (minor) -----------------------------------------
    vega = _net_vega(order)
    if order.strategy_name in SHORT_PREMIUM_STRATEGIES:
        if vega > 0:
            minor.append(
                f"short-premium order shows positive net vega ({vega:.2f}); double-check leg ratios"
            )
        if order.iv_rank < 30:
            minor.append(
                f"selling premium with iv_rank {order.iv_rank:.1f} is below the typical 30 floor"
            )
    if order.strategy_name in LONG_PREMIUM_STRATEGIES:
        if vega < 0:
            minor.append(
                f"long-premium order shows negative net vega ({vega:.2f}); legs may be inverted"
            )
        if order.iv_rank > 40:
            minor.append(
                f"buying premium with iv_rank {order.iv_rank:.1f} is above the typical 40 ceiling"
            )

    if not order.legs:
        # Zero-leg orders already flagged as major; skip net-premium
        # check to avoid duplicate noise.
        pass
    elif credit == 0:
        major.append("net premium is zero")

    if major:
        severity = "major"
    elif minor:
        severity = "minor"
    else:
        severity = "none"

    concerns = major + minor
    return {
        "concerns": concerns,
        "severity": severity,
        "major": major,
        "minor": minor,
        "net_theta": round(theta, 4),
        "net_vega": round(vega, 4),
        "net_credit": round(credit, 2),
    }
