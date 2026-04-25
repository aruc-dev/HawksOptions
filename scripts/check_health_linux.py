#!/usr/bin/env python3
"""Write a simple JSON health snapshot for the dashboard."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.config import load_config
from core.risk_manager import continuous_risk_checks
from scheduler.common import current_positions, load_runtime, refresh_positions


def main() -> int:
    config = load_config()
    _, client, paths = load_runtime(config)
    positions = refresh_positions(current_positions(paths), client=client)
    risk = continuous_risk_checks(positions, config=config)
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "overall_status": "green" if not risk["elevated_positions"] else "yellow",
        "stdout_tail": [f"open_positions={len(positions)}", f"elevated_positions={len(risk['elevated_positions'])}"],
        "portfolio_greeks": risk["portfolio_greeks"],
        "actions": risk["actions"],
    }
    snapshot_dir = paths["reports_dir"] / "health_snapshots"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    path = snapshot_dir / f"health_{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    print(path)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
