# HawksOptions — Codex Notes

Follow [AGENTS.md](/Users/arunbabuchandrababu/Desktop/AIPROJECTS/HawksOptions/AGENTS.md)
first. This file is the short Codex-specific version.

## Safe Workflow

1. Run unit tests.
2. Run the 30-day backtest.
3. Keep dashboard routes read-only.
4. Do not relax risk caps or change `mode` without approval.

## Useful Commands

```bash
python3 scheduler/run_scan.py --dry-run
python3 scheduler/run_risk_check.py --dry-run
python3 scheduler/run_roll_check.py --dry-run
python3 scheduler/run_backtest.py --days 30 --fund 10000
```
