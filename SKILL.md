# HawksOptions Repository Skill

Use this repository as the source of truth for:

- options contract selection by delta and DTE
- defined-risk order construction
- risk-manager fail-fast gates
- assignment and roll decision logic
- read-only options dashboard behavior

## Expected Workflow

1. Read `config/config.yaml`.
2. Confirm strategy parameters in `strategies/`.
3. Route every order through `core/risk_manager.py`.
4. Update tests before changing behavior.
5. Run `python3 -m unittest discover -v` and the bundled backtest.
