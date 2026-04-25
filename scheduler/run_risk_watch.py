"""Elevated one-minute risk monitoring."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.risk_manager import identify_elevated_positions
from scheduler.common import current_positions, load_runtime, refresh_positions


def run_risk_watch(*, config: dict | None = None) -> dict[str, object]:
    config, client, paths = load_runtime(config)
    positions = refresh_positions(current_positions(paths), client=client)
    flagged = identify_elevated_positions(positions, config=config)
    return {"elevated_count": len(flagged), "elevated_positions": flagged}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run elevated risk watch")
    parser.add_argument("--dry-run", action="store_true", help="Accepted for interface compatibility")
    parser.parse_args(argv)
    print(json.dumps(run_risk_watch(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
