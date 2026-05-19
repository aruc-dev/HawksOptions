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
    now = datetime.now(timezone.utc)
    as_of = now.date()
    positions = refresh_positions(current_positions(paths), client=client, as_of=as_of)
    flagged = identify_elevated_positions(positions, config=config)
    payload: dict[str, object] = {
        "generated_at": now.isoformat(timespec="seconds"),
        "elevated_count": len(flagged),
        "elevated_positions": flagged,
        "triggered_extra_risk_check": False,
        "risk_check": None,
    }
    if flagged:
        from scheduler.run_risk_check import _run_risk_check_with_runtime

        payload["triggered_extra_risk_check"] = True
        payload["risk_check"] = _run_risk_check_with_runtime(
            config=config,
            client=client,
            paths=paths,
            positions=positions,
            as_of=as_of,
            dry_run=True,
        )
    if not dry_run:
        paths["elevated_positions"].parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(paths["elevated_positions"], json.dumps(payload, indent=2))
        payload["metrics_path"] = str(write_metrics_textfile(paths["metrics"], risk_watch_metrics(payload)))
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run elevated risk watch")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        dest="dry_run",
        action="store_true",
        default=True,
        help="Run checks without persisting elevated-position snapshots or metrics.",
    )
    mode.add_argument(
        "--persist",
        dest="dry_run",
        action="store_false",
        help="Persist elevated-position snapshots and metrics.",
    )
    args = parser.parse_args(argv)
    print(json.dumps(run_risk_watch(dry_run=args.dry_run), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
