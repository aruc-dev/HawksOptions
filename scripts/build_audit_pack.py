#!/usr/bin/env python3
"""Build a sealed daily audit pack."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.audit_pack import build_audit_pack
from core.config import load_config
from scheduler.common import runtime_paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a HawksOptions daily audit pack")
    parser.add_argument("--date", default=date.today().isoformat(), help="trading day YYYY-MM-DD")
    parser.add_argument("--output-dir", default="reports/audit_packs")
    args = parser.parse_args(argv)

    trading_day = date.fromisoformat(args.date)
    config = load_config()
    paths = runtime_paths(config)
    pack = build_audit_pack(
        trading_day=trading_day,
        reports_dir=paths["reports_dir"],
        data_files=[paths["trade_log"], paths["positions"], paths["elevated_positions"]],
        output_dir=BASE_DIR / args.output_dir,
    )
    print(pack)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
