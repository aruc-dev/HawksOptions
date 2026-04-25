"""Generate an end-of-day markdown report."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.risk_manager import aggregate_portfolio_greeks
from core.trade_log import read_trade_rows
from scheduler.common import current_positions, load_runtime, refresh_positions


def generate_eod_report(*, config: dict | None = None, as_of: date | None = None) -> Path:
    as_of = as_of or date.today()
    config, client, paths = load_runtime(config)
    positions = refresh_positions(current_positions(paths), client=client, as_of=as_of)
    rows = read_trade_rows(paths["trade_log"])
    report_path = paths["reports_dir"] / f"eod_{as_of.isoformat()}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as handle:
        handle.write(f"# HawksOptions EOD Report — {as_of.isoformat()}\n\n")
        handle.write(f"- Open strategies: {len(positions)}\n")
        handle.write(f"- Portfolio value: ${client.get_account()['portfolio_value']:,.2f}\n")
        handle.write(f"- Trade-log rows: {len(rows)}\n")
        greeks = aggregate_portfolio_greeks(positions)
        handle.write(
            f"- Portfolio Greeks: delta={greeks['delta']}, theta={greeks['theta']}, "
            f"vega={greeks['vega']}, gamma={greeks['gamma']}\n"
        )
    return report_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate end-of-day report")
    parser.add_argument("--dry-run", action="store_true", help="Generate the report without extra side effects")
    parser.parse_args(argv)
    report_path = generate_eod_report()
    print(report_path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
