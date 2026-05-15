"""Quote freshness checks for pre-trade order validation."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any

from core.models import OptionContract, StrategyOrder


def quote_freshness_reasons(
    order: StrategyOrder,
    *,
    gates: dict[str, Any],
    as_of: date | datetime,
) -> list[str]:
    """Return quote-freshness rejection reasons for an order.

    Timestamp checks are opt-in via ``gates.max_quote_age_seconds`` or
    ``gates.reject_missing_quote_timestamp`` so existing sample-data tests
    stay backwards-compatible. Spread and zero-quote checks run whenever this
    helper is called because those are hard market-data quality failures.
    """
    reasons: list[str] = []
    max_age = _optional_positive_float(gates.get("max_quote_age_seconds"))
    reject_missing = bool(gates.get("reject_missing_quote_timestamp", max_age is not None))
    reject_lifecycle_stale = bool(gates.get("reject_stale_quote_fallback", max_age is not None))
    max_spread = _optional_positive_float(gates.get("max_bid_ask_spread_pct"))
    as_dt = _as_datetime(as_of)

    for leg in order.legs:
        contract = leg.contract
        if contract.bid <= 0 or contract.ask <= 0 or contract.mid_price() <= 0:
            reasons.append("invalid_quote")
        if max_spread is not None and contract.spread_pct() > max_spread:
            reasons.append("quote_spread_too_wide")
        if reject_lifecycle_stale and str(contract.meta.get("lifecycle_state", "")).lower() == "stale_quote_fallback":
            reasons.append("stale_quote_fallback")
        if max_age is None and not reject_missing:
            continue
        timestamp = quote_timestamp(contract)
        if timestamp is None:
            if reject_missing:
                reasons.append("missing_quote_timestamp")
            continue
        age_seconds = (as_dt - timestamp).total_seconds()
        if age_seconds < -1:
            reasons.append("future_quote_timestamp")
        elif max_age is not None and age_seconds > max_age:
            reasons.append("stale_quote")
    return _dedupe(reasons)


def quote_timestamp(contract: OptionContract) -> datetime | None:
    for key in ("quote_timestamp", "market_data_timestamp", "snapshot_timestamp", "updated_at"):
        value = contract.meta.get(key)
        timestamp = _parse_datetime(value)
        if timestamp is not None:
            return timestamp
    return None


def _as_datetime(value: date | datetime) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    return datetime.combine(value, time(16, 0), tzinfo=timezone.utc)


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value)
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _optional_positive_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _dedupe(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item not in seen:
            out.append(item)
            seen.add(item)
    return out
