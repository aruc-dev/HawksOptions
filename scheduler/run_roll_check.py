"""Check open strategies for roll candidates."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.roll_engine import should_roll_position
from scheduler.common import current_positions, load_runtime, refresh_positions


def run_roll_check(*, config: dict | None = None) -> dict[str, object]:
    config, client, paths = load_runtime(config)
    positions = refresh_positions(current_positions(paths), client=client)
    max_rolls = int(config.get("account", {}).get("max_rolls_per_strategy_id", 2))
    candidates = []
    for position in positions:
        decision = should_roll_position(position, max_rolls=max_rolls)
        if decision.should_roll:
            candidates.append({"strategy_id": position.strategy_id, "reason": decision.reason})
    return {"roll_candidates": candidates, "count": len(candidates)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run roll checks")
    parser.add_argument("--dry-run", action="store_true", help="Accepted for interface compatibility")
    parser.parse_args(argv)
    print(json.dumps(run_roll_check(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
