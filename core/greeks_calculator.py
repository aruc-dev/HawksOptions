"""Black-Scholes pricing and Greeks."""

from __future__ import annotations

import math
from dataclasses import dataclass


SQRT_2PI = math.sqrt(2.0 * math.pi)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / SQRT_2PI


def _time_sqrt(years_to_expiration: float) -> float:
    return math.sqrt(max(years_to_expiration, 1e-12))


def _d1(
    spot: float,
    strike: float,
    years_to_expiration: float,
    risk_free_rate: float,
    volatility: float,
    dividend_yield: float = 0.0,
) -> float:
    if spot <= 0 or strike <= 0 or volatility <= 0 or years_to_expiration <= 0:
        raise ValueError("spot, strike, volatility, and years_to_expiration must be positive")
    numerator = math.log(spot / strike) + (
        risk_free_rate - dividend_yield + 0.5 * volatility * volatility
    ) * years_to_expiration
    denominator = volatility * _time_sqrt(years_to_expiration)
    return numerator / denominator


def _d2(
    spot: float,
    strike: float,
    years_to_expiration: float,
    risk_free_rate: float,
    volatility: float,
    dividend_yield: float = 0.0,
) -> float:
    return _d1(
        spot=spot,
        strike=strike,
        years_to_expiration=years_to_expiration,
        risk_free_rate=risk_free_rate,
        volatility=volatility,
        dividend_yield=dividend_yield,
    ) - volatility * _time_sqrt(years_to_expiration)


def black_scholes_price(
    option_type: str,
    spot: float,
    strike: float,
    years_to_expiration: float,
    risk_free_rate: float,
    volatility: float,
    dividend_yield: float = 0.0,
) -> float:
    d1 = _d1(spot, strike, years_to_expiration, risk_free_rate, volatility, dividend_yield)
    d2 = _d2(spot, strike, years_to_expiration, risk_free_rate, volatility, dividend_yield)
    disc_r = math.exp(-risk_free_rate * years_to_expiration)
    disc_q = math.exp(-dividend_yield * years_to_expiration)
    if option_type == "call":
        return spot * disc_q * _norm_cdf(d1) - strike * disc_r * _norm_cdf(d2)
    if option_type == "put":
        return strike * disc_r * _norm_cdf(-d2) - spot * disc_q * _norm_cdf(-d1)
    raise ValueError(f"unsupported option_type {option_type!r}")


@dataclass(frozen=True)
class Greeks:
    price: float
    delta: float
    gamma: float
    theta: float
    vega: float


def black_scholes_greeks(
    option_type: str,
    spot: float,
    strike: float,
    years_to_expiration: float,
    risk_free_rate: float,
    volatility: float,
    dividend_yield: float = 0.0,
) -> Greeks:
    d1 = _d1(spot, strike, years_to_expiration, risk_free_rate, volatility, dividend_yield)
    d2 = _d2(spot, strike, years_to_expiration, risk_free_rate, volatility, dividend_yield)
    disc_r = math.exp(-risk_free_rate * years_to_expiration)
    disc_q = math.exp(-dividend_yield * years_to_expiration)
    pdf = _norm_pdf(d1)
    sqrt_t = _time_sqrt(years_to_expiration)
    price = black_scholes_price(
        option_type=option_type,
        spot=spot,
        strike=strike,
        years_to_expiration=years_to_expiration,
        risk_free_rate=risk_free_rate,
        volatility=volatility,
        dividend_yield=dividend_yield,
    )
    gamma = disc_q * pdf / (spot * volatility * sqrt_t)
    vega = spot * disc_q * pdf * sqrt_t
    if option_type == "call":
        delta = disc_q * _norm_cdf(d1)
        theta = (
            -(spot * disc_q * pdf * volatility) / (2.0 * sqrt_t)
            - risk_free_rate * strike * disc_r * _norm_cdf(d2)
            + dividend_yield * spot * disc_q * _norm_cdf(d1)
        )
    elif option_type == "put":
        delta = disc_q * (_norm_cdf(d1) - 1.0)
        theta = (
            -(spot * disc_q * pdf * volatility) / (2.0 * sqrt_t)
            + risk_free_rate * strike * disc_r * _norm_cdf(-d2)
            - dividend_yield * spot * disc_q * _norm_cdf(-d1)
        )
    else:
        raise ValueError(f"unsupported option_type {option_type!r}")
    return Greeks(price=price, delta=delta, gamma=gamma, theta=theta, vega=vega)
