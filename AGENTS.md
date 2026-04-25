# HawksOptions — AGENTS.md

Read this file before running commands or changing code in HawksOptions.

## Identity Check

- [ ] I have read/write access to `HawksOptions`
- [ ] I will not modify `config/config.yaml` risk caps without human approval
- [ ] I will not switch `mode` from `paper` to `live` without explicit approval
- [ ] I understand every order must be defined-risk before submission

## Core Rules

1. No naked short calls.
2. No naked short puts without full cash coverage.
3. No entries below 7 DTE.
4. Every order must pass `core.risk_manager.pre_trade_check`.
5. Earnings blackout and ex-dividend checks are mandatory.
6. The dashboard is read-only. Do not add mutation endpoints.

## Quick Commands

```bash
cd /Users/arunbabuchandrababu/Desktop/AIPROJECTS/HawksOptions
python3 -m unittest discover -v
python3 scheduler/run_scan.py --dry-run
python3 scheduler/run_risk_check.py --dry-run
python3 scheduler/run_backtest.py --days 30 --fund 10000
```

## Files That Matter

- Config: `config/config.yaml`
- Underlyings: `config/underlyings.yaml`
- Trade log: `data/trades.csv`
- Positions snapshot: `data/positions.json`
- Greeks snapshots: `data/greeks_snapshots/`
- Reports: `reports/`

## Validation Standard

Every logic change requires:

```bash
python3 -m unittest discover -v
python3 scheduler/run_backtest.py --days 30 --fund 10000
```

If either fails, fix the issue before handing work off.
