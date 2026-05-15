"""Placeholder interfaces for dealer-positioning / gamma-exposure context."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Protocol


@dataclass(frozen=True)
class DealerPositioningSnapshot:
    symbol: str
    as_of: date
    source: str = "unavailable"
    gamma_exposure: float | None = None
    gamma_flip_level: float | None = None
    regime: str = "unknown"

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["as_of"] = self.as_of.isoformat()
        return payload


class DealerPositioningProvider(Protocol):
    def snapshot(self, symbol: str, *, as_of: date) -> DealerPositioningSnapshot:
        """Return dealer-positioning context for ``symbol``."""


class NullDealerPositioningProvider:
    def snapshot(self, symbol: str, *, as_of: date) -> DealerPositioningSnapshot:
        return DealerPositioningSnapshot(symbol=symbol, as_of=as_of)


def dealer_positioning_context(
    underlying: dict[str, Any],
    *,
    as_of: date,
    underlying_price: float,
) -> dict[str, Any]:
    symbol = str(underlying.get("symbol", ""))
    gamma_exposure = _optional_float(underlying.get("gamma_exposure"))
    gamma_flip_level = _optional_float(underlying.get("gamma_flip_level"))
    if gamma_exposure is None and gamma_flip_level is None:
        return NullDealerPositioningProvider().snapshot(symbol, as_of=as_of).as_dict()
    regime = str(underlying.get("dealer_regime") or _infer_regime(gamma_exposure, gamma_flip_level, underlying_price))
    return DealerPositioningSnapshot(
        symbol=symbol,
        as_of=as_of,
        source=str(underlying.get("dealer_positioning_source", "underlying_metadata")),
        gamma_exposure=gamma_exposure,
        gamma_flip_level=gamma_flip_level,
        regime=regime,
    ).as_dict()


def _infer_regime(
    gamma_exposure: float | None,
    gamma_flip_level: float | None,
    underlying_price: float,
) -> str:
    if gamma_exposure is not None:
        if gamma_exposure > 0:
            return "positive_gamma"
        if gamma_exposure < 0:
            return "negative_gamma"
    if gamma_flip_level is not None and underlying_price > 0:
        return "above_flip" if underlying_price >= gamma_flip_level else "below_flip"
    return "unknown"


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
