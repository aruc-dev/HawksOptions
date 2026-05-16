# HawksOptions — Codex Notes

Follow [AGENTS.md](/Users/arunbabuchandrababu/Desktop/AIPROJECTS/HawksOptions/AGENTS.md)
first. This file is the short Codex-specific version.

## Safe Workflow

1. Run `bd ready --json` from the repo root before making changes.
2. If Beads is not initialized, run `./scripts/init_beads.sh`, then rerun
   `bd ready --json`.
3. Claim matching Beads work with `bd update <id> --claim --json`; create a
   task for non-trivial untracked work.
4. Add focused unit tests for every functional change.
5. Run the full unit-test suite.
6. Run lint checks.
7. Run the 30-day deterministic backtest.
8. Keep dashboard routes read-only.
9. Do not relax risk caps or change `mode` without approval.
10. When the user says "Learning for you", update the relevant Markdown
    guidance files with the instruction that follows so it persists across
    future sessions.
11. When the user provides a pull request link prefixed with "PR:", inspect
    review comments, fix valid actionable comments, validate the changes, and
    resolve the comments only after everything is clean.

## Useful Commands

```bash
bd ready --json
bd update <id> --claim --json
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

Always start with `bd ready --json`. If this checkout is not initialized for
`bd` (Beads), run `./scripts/init_beads.sh`, then rerun `bd ready --json`.
Claim matching work with `bd update <id> --claim --json`; if no matching task
exists for non-trivial work, create one with `bd create "<title>" -t task -p 2 --json`.

Bootstrap once with:

```bash
./scripts/init_beads.sh
```
