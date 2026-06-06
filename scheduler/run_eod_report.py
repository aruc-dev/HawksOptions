"""Generate an end-of-day markdown report."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.risk_manager import aggregate_portfolio_greeks
from core.trade_log import read_trade_rows
from dashboard.pnl import realized_pnl_for_row, realized_pnl_today, realized_pnl_window, slippage_summary, strategy_summary
from dashboard.data_sources import read_latest_ai_disagreements, read_latest_candidate_scan, read_latest_strategy_attribution
from scheduler.common import current_positions, load_runtime, refresh_positions


def generate_eod_report(*, config: dict | None = None, as_of: date | None = None) -> Path:
    as_of = as_of or date.today()
    config, client, paths = load_runtime(config)
    positions = refresh_positions(current_positions(paths), client=client, as_of=as_of)
    rows = read_trade_rows(paths["trade_log"])
    account = client.get_account()
    now_utc = datetime.combine(as_of, datetime.max.time(), tzinfo=timezone.utc)
    today = realized_pnl_today(rows, now_utc=now_utc)
    window = realized_pnl_window(rows, lookback_days=30, now_utc=now_utc)
    by_strategy = strategy_summary(rows, lookback_days=30, now_utc=now_utc)
    by_symbol = _symbol_summary(rows, lookback_days=30, now_utc=now_utc)
    slippage = slippage_summary(rows, lookback_days=30, now_utc=now_utc)
    latest_scan = read_latest_candidate_scan(paths["reports_dir"])
    rejection_counts = _rejection_counts(latest_scan)
    ai_disagreements = read_latest_ai_disagreements(paths["reports_dir"])
    latest_attribution = read_latest_strategy_attribution(paths["reports_dir"])
    report_path = paths["reports_dir"] / f"eod_{as_of.isoformat()}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(f"# HawksOptions EOD Report — {as_of.isoformat()}\n\n")
        handle.write(f"- Open strategies: {len(positions)}\n")
        handle.write(f"- Portfolio value: ${float(account['portfolio_value']):,.2f}\n")
        handle.write(f"- Trade-log rows: {len(rows)}\n")
        greeks = aggregate_portfolio_greeks(positions)
        handle.write(
            f"- Portfolio Greeks: delta={greeks['delta']}, theta={greeks['theta']}, "
            f"vega={greeks['vega']}, gamma={greeks['gamma']}\n"
        )
        handle.write(f"- Realized PnL today: ${today['total_usd']:,.2f} across {today['trade_count']} closed legs\n")
        handle.write(
            f"- Realized PnL 30d: ${window['total_usd']:,.2f}, "
            f"wins={window['wins']}, losses={window['losses']}\n"
        )

        handle.write("\n## Strategy PnL 30d\n\n")
        if by_strategy:
            for row in by_strategy:
                handle.write(
                    f"- {row['strategy']}: PnL ${row['total_usd']:,.2f}, "
                    f"legs={row['count']}, win_rate={row['win_rate']:.2%}\n"
                )
        else:
            handle.write("- No closed strategy rows in the 30-day window.\n")

        handle.write("\n## Symbol PnL 30d\n\n")
        if by_symbol:
            for row in by_symbol:
                handle.write(
                    f"- {row['underlying']}: PnL ${row['total_usd']:,.2f}, "
                    f"legs={row['count']}, win_rate={row['win_rate']:.2%}\n"
                )
        else:
            handle.write("- No closed symbol rows in the 30-day window.\n")

        handle.write("\n## Slippage 30d\n\n")
        if slippage:
            for row in slippage:
                handle.write(
                    f"- {row['strategy']}: total ${row['total_slippage_dollars']:,.2f}, "
                    f"avg ${row['average_slippage_dollars']:,.4f} across {row['leg_count']} legs\n"
                )
        else:
            handle.write("- No slippage rows available.\n")

        handle.write("\n## Rejections And AI\n\n")
        if rejection_counts:
            for reason, count in rejection_counts.most_common():
                handle.write(f"- {reason}: {count}\n")
        else:
            handle.write("- No rejection summary available.\n")
        disagreement_count = (
            ai_disagreements.get("data", {}).get("summary", {}).get("total", 0)
            if isinstance(ai_disagreements.get("data"), dict)
            else 0
        )
        handle.write(f"- AI disagreements: {disagreement_count}\n")

        if latest_attribution.get("ok"):
            handle.write("\n## Latest Backtest Attribution Snapshot\n\n")
            handle.write("```json\n")
            handle.write(json.dumps(latest_attribution.get("data", {}), indent=2, sort_keys=True, default=str))
            handle.write("\n```\n")
    return report_path


def _symbol_summary(rows: list[dict[str, Any]], *, lookback_days: int, now_utc: datetime) -> list[dict[str, Any]]:
    cutoff = now_utc - timedelta(days=max(1, int(lookback_days)))
    buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"underlying": "unknown", "count": 0, "wins": 0, "losses": 0, "total_usd": 0.0})
    for row in rows:
        if str(row.get("status", "")).lower() != "closed":
            continue
        timestamp = _parse_iso(str(row.get("close_timestamp", "") or row.get("timestamp", "")))
        if timestamp is None or timestamp < cutoff:
            continue
        symbol = str(row.get("underlying", "unknown"))
        bucket = buckets[symbol]
        bucket["underlying"] = symbol
        bucket["count"] += 1
        pnl = realized_pnl_for_row(row)
        bucket["total_usd"] += pnl
        if pnl > 0:
            bucket["wins"] += 1
        elif pnl < 0:
            bucket["losses"] += 1
    out = []
    for bucket in buckets.values():
        count = bucket["count"] or 1
        bucket["win_rate"] = round(bucket["wins"] / count, 4)
        bucket["total_usd"] = round(bucket["total_usd"], 2)
        out.append(bucket)
    return sorted(out, key=lambda item: item["underlying"])


def _rejection_counts(candidate_scan: dict[str, Any]) -> Counter[str]:
    payload = candidate_scan.get("data") if isinstance(candidate_scan, dict) else None
    out: Counter[str] = Counter()
    if not isinstance(payload, dict):
        return out
    for item in payload.get("rejected", []):
        if not isinstance(item, dict):
            continue
        for reason in item.get("reasons", []):
            out[str(reason)] += 1
    return out


def _parse_iso(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate end-of-day report")
    parser.add_argument("--dry-run", action="store_true", help="Generate the report without extra side effects")
    parser.parse_args(argv)
    report_path = generate_eod_report()
    print(report_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
