"""Helpers for OCC option symbols."""

from __future__ import annotations

from datetime import date


def format_occ_symbol(
    underlying: str,
    expiration: date,
    option_type: str,
    strike: float,
) -> str:
    root = str(underlying).upper().replace("/", "")
    cp = "C" if option_type == "call" else "P"
    strike_component = f"{int(round(float(strike) * 1000)):08d}"
    return f"{root}{expiration:%y%m%d}{cp}{strike_component}"


def parse_occ_symbol(symbol: str) -> dict[str, object]:
    text = str(symbol).strip().upper()
    if len(text) < 15:
        raise ValueError(f"invalid OCC symbol {symbol!r}")
    root = text[:-15]
    expiration = date.fromisoformat(f"20{text[-15:-13]}-{text[-13:-11]}-{text[-11:-9]}")
    option_type = "call" if text[-9] == "C" else "put"
    strike = int(text[-8:]) / 1000.0
    return {
        "underlying": root,
        "expiration": expiration,
        "option_type": option_type,
        "strike": strike,
    }
