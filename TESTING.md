# HawksOptions Testing

## Required Checks

Functional changes must include focused unit tests for the behavior being added
or changed. Run the full validation gate before publishing or deploying:

```bash
python3 -m unittest discover -v
python3 -W error::DeprecationWarning -m unittest discover
ruff check .
python3 -m compileall core strategies scheduler ai tests dashboard scripts
python3 scheduler/run_backtest.py --days 30 --fund 10000
```

## Test Areas

- pricing and Greeks
- IV rank calculations
- contract filtering and strike selection
- risk-manager pre-trade gates
- assignment and roll decisions
- dashboard auth and read-only behavior
- scheduler dry-run paths
