"""Event-date freshness checks for configured underlyings."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timezone
from typing import Any

EVENT_DATE_FIELDS = {
    "next_earnings_date": "earnings",
    "ex_dividend_date": "ex_dividend",
}
EVENT_UPDATED_AT_FIELDS = {
    "next_earnings_date": ("earnings_date_updated_at", "event_data_updated_at"),
    "ex_dividend_date": ("ex_dividend_date_updated_at", "event_data_updated_at"),
}


def event_data_max_age_days(config: dict[str, Any]) -> int:
    underlyings_cfg = config.get("underlyings", {}) if isinstance(config, dict) else {}
    if not isinstance(underlyings_cfg, dict):
        return 7
    value = underlyings_cfg.get("refresh_earnings_dates_days", 7)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 7


def event_data_freshness_report(
    underlyings: list[dict[str, Any]],
    *,
    config: dict[str, Any],
    as_of: date | None = None,
) -> dict[str, Any]:
    """Return scan-safe event metadata plus stale-date diagnostics.

    Past event dates are cleared from the sanitized copy so strategy gates do
    not keep acting on old earnings or dividend metadata. If optional
    ``*_updated_at`` metadata exists, the report also flags refreshes older
    than ``underlyings.refresh_earnings_dates_days``.
    """
    as_of = as_of or date.today()
    max_age_days = event_data_max_age_days(config)
    sanitized_underlyings: list[dict[str, Any]] = []
    stale_events: list[dict[str, Any]] = []
    refresh_overdue: list[dict[str, Any]] = []
    checked_count = 0

    for item in underlyings:
        sanitized = deepcopy(item)
        symbol = str(item.get("symbol", "")).upper()
        for field, event_type in EVENT_DATE_FIELDS.items():
            raw_value = item.get(field)
            if raw_value in (None, ""):
                continue
            checked_count += 1
            event_date = _parse_date(raw_value)
            if event_date is None:
                stale_events.append(
                    {
                        "symbol": symbol,
                        "field": field,
                        "event_type": event_type,
                        "value": str(raw_value),
                        "reason": "invalid_date",
                    }
                )
                sanitized[field] = None
                continue
            if event_date < as_of:
                stale_events.append(
                    {
                        "symbol": symbol,
                        "field": field,
                        "event_type": event_type,
                        "value": event_date.isoformat(),
                        "reason": "past_event_date",
                        "days_stale": (as_of - event_date).days,
                    }
                )
                sanitized[field] = None
                continue
            updated_at = _first_datetime(item, EVENT_UPDATED_AT_FIELDS[field])
            if updated_at is not None and max_age_days > 0:
                age_days = (as_of - updated_at.date()).days
                if age_days > max_age_days:
                    refresh_overdue.append(
                        {
                            "symbol": symbol,
                            "field": field,
                            "event_type": event_type,
                            "value": event_date.isoformat(),
                            "reason": "refresh_overdue",
                            "updated_at": updated_at.isoformat(),
                            "age_days": age_days,
                            "max_age_days": max_age_days,
                        }
                    )
        sanitized_underlyings.append(sanitized)

    return {
        "as_of": as_of.isoformat(),
        "max_age_days": max_age_days,
        "checked_event_count": checked_count,
        "stale_event_count": len(stale_events),
        "refresh_overdue_count": len(refresh_overdue),
        "stale_events": stale_events,
        "refresh_overdue": refresh_overdue,
        "affected_symbols": sorted(
            {
                str(item.get("symbol", ""))
                for item in [*stale_events, *refresh_overdue]
                if item.get("symbol")
            }
        ),
        "status": "ok" if not stale_events and not refresh_overdue else "stale",
        "sanitized_underlyings": sanitized_underlyings,
    }


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, datetime.min.time())
    elif value not in (None, ""):
        text = str(value)
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _first_datetime(item: dict[str, Any], fields: tuple[str, ...]) -> datetime | None:
    for field in fields:
        parsed = _parse_datetime(item.get(field))
        if parsed is not None:
            return parsed
    return None
