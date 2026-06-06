"""Check configured event metadata for stale earnings/dividend dates."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.config import ensure_runtime_dirs, load_config, load_underlyings, reporting_path
from core.event_freshness import event_data_freshness_report
from core.file_lock import atomic_write_text


def check_event_freshness(*, config: dict[str, Any], as_of: date | None = None) -> dict[str, Any]:
    as_of = as_of or date.today()
    underlyings = load_underlyings(config)
    report = event_data_freshness_report(underlyings, config=config, as_of=as_of)
    return {key: value for key, value in report.items() if key != "sanitized_underlyings"}


def persist_event_freshness_report(*, config: dict[str, Any], payload: dict[str, Any]) -> dict[str, str]:
    ensure_runtime_dirs(config)
    report_dir = reporting_path(config, "reports_dir") / "event_data_freshness"
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc)
    stem = f"event_data_freshness_{payload['as_of']}_{timestamp:%H%M%S%f}Z"
    json_path = report_dir / f"{stem}.json"
    markdown_path = report_dir / f"{stem}.md"
    atomic_write_text(json_path, json.dumps(payload, indent=2, sort_keys=True), lock=False)
    atomic_write_text(markdown_path, _markdown_report(payload), lock=False)
    return {
        "json_report_path": str(json_path),
        "markdown_report_path": str(markdown_path),
    }


def _markdown_report(payload: dict[str, Any]) -> str:
    lines = [
        "# HawksOptions Event Data Freshness",
        "",
        f"- As of: {payload['as_of']}",
        f"- Status: {payload['status']}",
        f"- Checked events: {payload['checked_event_count']}",
        f"- Stale events: {payload['stale_event_count']}",
        f"- Refresh overdue: {payload['refresh_overdue_count']}",
        f"- Max age days: {payload['max_age_days']}",
        f"- Affected symbols: {', '.join(payload['affected_symbols']) or 'none'}",
        "",
        "## Stale Events",
        "",
    ]
    stale_events = list(payload.get("stale_events", []))
    if stale_events:
        for item in stale_events:
            lines.append(
                f"- {item.get('symbol', '')} {item.get('field', '')}: "
                f"{item.get('reason', '')} ({item.get('value', 'n/a')})"
            )
    else:
        lines.append("- none")
    lines.extend(["", "## Refresh Overdue", ""])
    overdue = list(payload.get("refresh_overdue", []))
    if overdue:
        for item in overdue:
            lines.append(
                f"- {item.get('symbol', '')} {item.get('field', '')}: "
                f"age {item.get('age_days', 'n/a')}d > {item.get('max_age_days', 'n/a')}d "
                f"({item.get('value', 'n/a')})"
            )
    else:
        lines.append("- none")
    lines.extend(["", "```json", json.dumps(payload, indent=2, sort_keys=True), "```", ""])
    return "\n".join(lines)


def _parse_as_of(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check HawksOptions configured event-date freshness")
    parser.add_argument("--as-of", help="Check date in YYYY-MM-DD format; defaults to today")
    parser.add_argument(
        "--fail-on-stale",
        action="store_true",
        help="Exit non-zero when stale or refresh-overdue event metadata is found",
    )
    parser.add_argument("--no-report", action="store_true", help="Print JSON only; do not persist reports")
    args = parser.parse_args(argv)

    config = load_config()
    payload = check_event_freshness(config=config, as_of=_parse_as_of(args.as_of))
    if not args.no_report:
        payload = {**payload, **persist_event_freshness_report(config=config, payload=payload)}
    print(json.dumps(payload, indent=2, sort_keys=True))
    if args.fail_on_stale and (
        int(payload.get("stale_event_count", 0)) > 0
        or int(payload.get("refresh_overdue_count", 0)) > 0
    ):
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
