# HawksOptions Repository Skill

Use this repository as the source of truth for:

- options contract selection by delta and DTE
- defined-risk order construction
- risk-manager fail-fast gates
- assignment and roll decision logic
- read-only options dashboard behavior

## Expected Workflow

1. Run `bd ready --json` from the repo root.
2. If Beads is not initialized, run `./scripts/init_beads.sh`, then rerun
   `bd ready --json`.
3. Claim matching work with `bd update <id> --claim --json`, or create a Beads
   task for non-trivial untracked work.
4. Read `config/config.yaml`.
5. Confirm strategy parameters in `strategies/`.
6. Route every order through `core/risk_manager.py`.
7. Update tests before changing behavior.
8. Add focused unit tests for every functional change.
9. Run `python3 -m unittest discover -v`.
10. Run `python3 -W error::DeprecationWarning -m unittest discover`.
11. Run `ruff check .`.
12. Run the bundled deterministic backtest.
