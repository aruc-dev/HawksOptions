"""NBBO snapshot helpers for execution-quality auditing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.models import StrategyOrder


def capture_nbbo_snapshot(client: Any, order: StrategyOrder) -> dict[str, Any]:
    symbols = [leg.contract.contract_symbol for leg in order.legs]
    quote_map = _quotes_from_client(client, symbols)
    legs = []
    for leg in order.legs:
        symbol = leg.contract.contract_symbol
        quote = quote_map.get(symbol, {})
        bid = _float_or_none(quote.get("bid"))
        ask = _float_or_none(quote.get("ask"))
        source = str(quote.get("source", "client_quote"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or ask < bid:
            bid = float(leg.contract.bid)
            ask = float(leg.contract.ask)
            source = "order_contract"
        midpoint = round((bid + ask) / 2.0, 4) if bid > 0 and ask > 0 else float(leg.contract.mid_price())
        legs.append(
            {
                "contract_symbol": symbol,
                "bid": round(bid, 4),
                "ask": round(ask, 4),
                "midpoint": midpoint,
                "source": source,
            }
        )
    snapshot = {
        "captured_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "legs": legs,
    }
    order.metadata["nbbo_snapshot"] = snapshot
    return snapshot


def expected_leg_midpoint(order: StrategyOrder, contract_symbol: str) -> float | None:
    snapshot = order.metadata.get("nbbo_snapshot")
    if not isinstance(snapshot, dict):
        return None
    for item in snapshot.get("legs", []):
        if not isinstance(item, dict):
            continue
        if str(item.get("contract_symbol")) != contract_symbol:
            continue
        value = _float_or_none(item.get("midpoint"))
        return round(value, 4) if value is not None else None
    return None


def has_complete_client_nbbo(snapshot: dict[str, Any]) -> bool:
    legs = snapshot.get("legs", [])
    if not isinstance(legs, list) or not legs:
        return False
    for item in legs:
        if not isinstance(item, dict) or item.get("source") == "order_contract":
            return False
        bid = _float_or_none(item.get("bid"))
        ask = _float_or_none(item.get("ask"))
        midpoint = _float_or_none(item.get("midpoint"))
        if bid is None or ask is None or midpoint is None or bid <= 0 or ask <= 0 or midpoint <= 0 or ask < bid:
            return False
    return True


def _quotes_from_client(client: Any, symbols: list[str]) -> dict[str, dict[str, Any]]:
    getter = getattr(client, "get_option_quotes", None)
    if not callable(getter):
        return {}
    try:
        raw = getter(symbols)
    except NotImplementedError:
        return {}
    if isinstance(raw, dict):
        return {
            str(symbol): quote
            for symbol, quote in raw.items()
            if isinstance(quote, dict)
        }
    return {}


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
