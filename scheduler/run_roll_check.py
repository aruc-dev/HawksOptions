"""Check open strategies for roll candidates."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.risk_manager import pre_trade_check
from core.roll_engine import build_roll_plan, should_roll_position
from scheduler.common import build_context, configured_underlyings, current_positions, load_runtime, refresh_positions
from strategies import build_enabled_strategies


def run_roll_check(*, config: dict | None = None, as_of: date | None = None) -> dict[str, object]:
    as_of = as_of or date.today()
    config, client, paths = load_runtime(config)
    positions = refresh_positions(current_positions(paths), client=client, as_of=as_of)
    account = client.get_account()
    max_rolls = int(config.get("account", {}).get("max_rolls_per_strategy_id", 2))
    strategies = {strategy.name: strategy for strategy in build_enabled_strategies(config)}
    underlyings = {item["symbol"]: item for item in configured_underlyings(config)}
    candidates = []
    for position in positions:
        decision = should_roll_position(position, max_rolls=max_rolls)
        if decision.should_roll:
            candidates.append(
                {
                    "strategy_id": position.strategy_id,
                    "reason": decision.reason,
                    "roll_plan": _replacement_roll_plan(
                        position,
                        strategy=strategies.get(position.strategy_name),
                        underlying=underlyings.get(position.underlying),
                        config=config,
                        client=client,
                        account=account,
                        open_positions=positions,
                        as_of=as_of,
                    ),
                }
            )
    return {"roll_candidates": candidates, "count": len(candidates)}


def _replacement_roll_plan(
    position,
    *,
    strategy,
    underlying,
    config: dict,
    client,
    account: dict,
    open_positions: list,
    as_of: date,
):
    if strategy is None or underlying is None:
        return None
    context = build_context(
        config=config,
        client=client,
        underlying=underlying,
        account=account,
        open_positions=[item for item in open_positions if item.strategy_id != position.strategy_id],
        as_of=as_of,
    )
    replacement = strategy.generate_order(context)
    if replacement is None:
        return None
    decision = pre_trade_check(
        replacement,
        account=account,
        config=config,
        open_positions=[item for item in open_positions if item.strategy_id != position.strategy_id],
        as_of=as_of,
    )
    if not decision.accepted:
        return {"status": "rejected", "reasons": decision.reasons}
    plan = build_roll_plan(position, replacement)
    if plan is None:
        return {"status": "rejected", "reasons": ["roll_not_net_credit"]}
    return {"status": "planned", **plan}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run roll checks")
    parser.add_argument("--dry-run", action="store_true", help="Accepted for interface compatibility")
    parser.parse_args(argv)
    print(json.dumps(run_roll_check(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
