"""Baseline five-minute risk checks."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.close_executor import close_order_plans
from core.order_executor import save_positions
from core.risk_manager import (
    continuous_risk_checks,
    daily_loss_status,
    read_daily_baseline,
    write_daily_baseline,
    write_greeks_snapshot,
)
from scheduler.common import current_positions, load_runtime, refresh_positions


def run_risk_check(*, config: dict | None = None, as_of: date | None = None, dry_run: bool = True) -> dict[str, object]:
    as_of = as_of or date.today()
    config, client, paths = load_runtime(config)
    positions = refresh_positions(current_positions(paths), client=client, as_of=as_of)
    save_positions(paths["positions"], positions)
    payload = continuous_risk_checks(
        positions,
        config=config,
        as_of=datetime.combine(as_of, time(16, 0), tzinfo=timezone.utc),
    )
    risk_actions = config.get("risk_actions", {}) if isinstance(config, dict) else {}
    execute_closes = bool(risk_actions.get("execute_closes", False)) if isinstance(risk_actions, dict) else False
    payload["close_orders"] = close_order_plans(
        positions,
        payload.get("actions", []),
        client=client,
        execute_enabled=execute_closes,
        dry_run=dry_run,
    )
    positions = [position for position in positions if position.closed_at is None]
    if execute_closes and not dry_run and payload["close_orders"]:
        save_positions(paths["positions"], positions)
    baseline = read_daily_baseline(paths["baseline"])
    if baseline is None or baseline.get("date") != as_of.isoformat():
        baseline = write_daily_baseline(paths["baseline"], client.get_account()["portfolio_value"], as_of=datetime.combine(as_of, time(13, 31), tzinfo=timezone.utc))
    payload["daily_loss"] = daily_loss_status(
        float(baseline["portfolio_value"]),
        float(client.get_account()["portfolio_value"]),
        halt_pct=float(config.get("account", {}).get("daily_loss_halt_pct", 0.05)),
        hard_close_pct=float(config.get("account", {}).get("tail_risk_close_pct", 0.08)),
    )
    snapshot_path = write_greeks_snapshot(paths["greeks_dir"], payload, as_of=datetime.now(timezone.utc))
    payload["snapshot_path"] = str(snapshot_path)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run HawksOptions risk checks")
    parser.add_argument("--dry-run", action="store_true", help="Accepted for interface compatibility")
    args = parser.parse_args(argv)
    result = run_risk_check(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
