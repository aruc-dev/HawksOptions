"""Run broker/local state reconciliation explicitly."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from core.alpaca_options_client import AlpacaOptionsClient
from core.config import ensure_runtime_dirs, load_config
from core.reconciler import reconcile_state
from scheduler.common import runtime_paths


def run_reconcile(*, config: dict | None = None) -> dict[str, object]:
    config = config or load_config()
    ensure_runtime_dirs(config)
    paths = runtime_paths(config)
    client = AlpacaOptionsClient(config)
    return reconcile_state(
        client=client,
        positions_path=paths["positions"],
        reports_dir=paths["reports_dir"],
        halt_file=paths["halt_file"],
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run HawksOptions broker/local state reconciliation")
    parser.parse_args(argv)
    print(json.dumps(run_reconcile(), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
