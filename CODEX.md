# HawksOptions — Codex Notes

Follow [AGENTS.md](/Users/arunbabuchandrababu/Desktop/AIPROJECTS/HawksOptions/AGENTS.md)
first. This file is the short Codex-specific version.

## Safe Workflow

1. Add focused unit tests for every functional change.
2. Run the full unit-test suite.
3. Run lint checks.
4. Run the 30-day deterministic backtest.
5. Keep dashboard routes read-only.
6. Do not relax risk caps or change `mode` without approval.

## Useful Commands

```bash
python3 -m unittest discover -v
python3 -W error::DeprecationWarning -m unittest discover
ruff check .
python3 -m compileall core strategies scheduler ai tests dashboard scripts
python3 scheduler/run_scan.py --dry-run
python3 scheduler/run_risk_check.py --dry-run
python3 scheduler/run_roll_check.py --dry-run
python3 scheduler/run_backtest.py --days 30 --fund 10000
```

## Beads

If this checkout is initialized for `bd` (Beads), start with `bd ready --json`
and claim work with `bd update <id> --claim --json`.

Bootstrap once with:

```bash
./scripts/init_beads.sh
```
