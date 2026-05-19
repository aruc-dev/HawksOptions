"""Baseline five-minute risk checks."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.close_executor import close_order_plans, reconcile_pending_closes
from core.metrics import risk_check_metrics, write_metrics_textfile
from core.order_executor import save_positions
from core.risk_manager import (
    continuous_risk_checks,
    daily_loss_status,
    read_daily_baseline,
    write_daily_baseline,
    write_greeks_snapshot,
)
from core.trade_log import mark_strategy_closed
from scheduler.common import current_positions, load_runtime, refresh_positions


def run_risk_check(*, config: dict | None = None, as_of: date | None = None, dry_run: bool = True) -> dict[str, object]:
    as_of = as_of or date.today()
    config, client, paths = load_runtime(config)
    positions = refresh_positions(current_positions(paths), client=client, as_of=as_of)
    return _run_risk_check_with_runtime(
        config=config,
        client=client,
        paths=paths,
        positions=positions,
        as_of=as_of,
        dry_run=dry_run,
    )


def _run_risk_check_with_runtime(
    *,
    config: dict,
    client,
    paths: dict[str, Path],
    positions: list,
    as_of: date,
    dry_run: bool,
) -> dict[str, object]:
    pending_close_reconciliations = reconcile_pending_closes(positions, client=client)
    closed_positions = [position for position in positions if position.closed_at is not None]
    positions = [position for position in positions if position.closed_at is None]
    payload = continuous_risk_checks(
        positions,
        config=config,
        as_of=datetime.combine(as_of, time(16, 0), tzinfo=timezone.utc),
    )
    risk_actions = config.get("risk_actions", {}) if isinstance(config, dict) else {}
    execute_closes = bool(risk_actions.get("execute_closes", False)) if isinstance(risk_actions, dict) else False
    allowed_auto_close_actions = risk_actions.get("allowed_auto_close_actions", ["stop_loss", "close_for_ex_div"]) if isinstance(risk_actions, dict) else []
    payload["pending_close_reconciliations"] = pending_close_reconciliations
    payload["close_orders"] = close_order_plans(
        positions,
        payload.get("actions", []),
        client=client,
        execute_enabled=execute_closes,
        dry_run=dry_run,
        allowed_auto_close_actions=allowed_auto_close_actions,
    )
    closed_positions.extend(position for position in positions if position.closed_at is not None)
    if not dry_run:
        for position in closed_positions:
            mark_strategy_closed(
                paths["trade_log"],
                position,
                exit_reason=position.close_action or "risk_close",
                closed_at=position.closed_at,
            )
    positions = [position for position in positions if position.closed_at is None]
    if not dry_run:
        save_positions(paths["positions"], positions)
    account = client.get_account()
    baseline = read_daily_baseline(paths["baseline"])
    if baseline is None or baseline.get("date") != as_of.isoformat():
        if dry_run:
            baseline = {
                "date": as_of.isoformat(),
                "portfolio_value": float(account["portfolio_value"]),
                "timestamp": datetime.combine(as_of, time(13, 31), tzinfo=timezone.utc).isoformat(timespec="seconds"),
            }
        else:
            baseline = write_daily_baseline(
                paths["baseline"],
                account["portfolio_value"],
                as_of=datetime.combine(as_of, time(13, 31), tzinfo=timezone.utc),
            )
    payload["daily_loss"] = daily_loss_status(
        float(baseline["portfolio_value"]),
        float(account["portfolio_value"]),
        halt_pct=float(config.get("account", {}).get("daily_loss_halt_pct", 0.05)),
        hard_close_pct=float(config.get("account", {}).get("tail_risk_close_pct", 0.08)),
    )
    if dry_run:
        payload["snapshot_path"] = ""
    else:
        snapshot_path = write_greeks_snapshot(paths["greeks_dir"], payload, as_of=datetime.now(timezone.utc))
        payload["snapshot_path"] = str(snapshot_path)
        if "metrics" in paths:
            payload["metrics_path"] = str(write_metrics_textfile(paths["metrics"], risk_check_metrics(payload)))
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
