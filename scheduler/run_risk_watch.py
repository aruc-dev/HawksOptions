"""Elevated one-minute risk monitoring."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.file_lock import atomic_write_text
from core.metrics import risk_watch_metrics, write_metrics_textfile
from core.risk_manager import identify_elevated_positions
from scheduler.common import current_positions, load_runtime, refresh_positions


def run_risk_watch(*, config: dict | None = None, dry_run: bool = True) -> dict[str, object]:
    config, client, paths = load_runtime(config)
    positions = refresh_positions(current_positions(paths), client=client)
    flagged = identify_elevated_positions(positions, config=config)
    payload: dict[str, object] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "elevated_count": len(flagged),
        "elevated_positions": flagged,
        "triggered_extra_risk_check": False,
        "risk_check": None,
    }
    if flagged:
        from scheduler.run_risk_check import run_risk_check

        payload["triggered_extra_risk_check"] = True
        payload["risk_check"] = run_risk_check(config=config, dry_run=True)
    if not dry_run:
        paths["elevated_positions"].parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(paths["elevated_positions"], json.dumps(payload, indent=2))
        payload["metrics_path"] = str(write_metrics_textfile(paths["metrics"], risk_watch_metrics(payload)))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run elevated risk watch")
    parser.add_argument("--dry-run", action="store_true", help="Accepted for interface compatibility")
    args = parser.parse_args(argv)
    print(json.dumps(run_risk_watch(dry_run=args.dry_run), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
