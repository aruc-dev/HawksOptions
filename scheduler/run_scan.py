"""Run a scan across the configured watchlist."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from typing import Any
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from ai.trade_idea_critic import critique_trade
from core.order_executor import execute_order, persist_open_order
from core.risk_manager import pre_trade_check
from scheduler.common import build_context, configured_underlyings, current_positions, load_runtime
from strategies import build_enabled_strategies


def scan_market(*, config: dict[str, Any], as_of: date | None = None, dry_run: bool = True) -> dict[str, Any]:
    as_of = as_of or date.today()
    config, client, paths = load_runtime(config)
    account = client.get_account()
    positions = current_positions(paths)
    strategies = build_enabled_strategies(config)
    accepted = []
    rejected = []
    for underlying in configured_underlyings(config):
        context = build_context(
            config=config,
            client=client,
            underlying=underlying,
            account=account,
            open_positions=positions,
            as_of=as_of,
        )
        for strategy in strategies:
            order = strategy.generate_order(context)
            if order is None:
                continue
            critique = critique_trade(order)
            if critique.get("severity") == "major":
                order.ai_veto_reason = "trade_critic_major_concern"
            decision = pre_trade_check(
                order,
                account=account,
                config=config,
                open_positions=positions,
                as_of=as_of,
            )
            if not decision.accepted:
                rejected.append(
                    {
                        "underlying": underlying["symbol"],
                        "strategy": strategy.name,
                        "reasons": decision.reasons,
                    }
                )
                continue
            result = execute_order(client, order, dry_run=dry_run)
            accepted.append({"underlying": underlying["symbol"], "strategy": strategy.name, "order": result})
            if not dry_run:
                position = persist_open_order(
                    order=order,
                    mode=str(config.get("mode", "paper")),
                    order_id=str(result["id"]),
                    trade_log_path=paths["trade_log"],
                    positions_path=paths["positions"],
                )
                positions.append(position)
            break
    return {
        "as_of": as_of.isoformat(),
        "accepted_count": len(accepted),
        "rejected_count": len(rejected),
        "accepted": accepted,
        "rejected": rejected,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the HawksOptions market scan")
    parser.add_argument("--dry-run", action="store_true", help="Generate orders but do not persist them")
    args = parser.parse_args(argv)
    result = scan_market(config={}, dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
