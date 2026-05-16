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

## Persistent User Learnings

- When the user says "Learning for you", treat the instruction that follows as
  persistent repo guidance. Update the relevant Markdown reference files, such
  as `AGENTS.md`, `CODEX.md`, or other agent-facing docs, so future sessions can
  apply it.
- If the learning changes workflow or review behavior, place it in the
  agent-operating docs rather than only acknowledging it in chat.
- When the user provides a pull request link prefixed with "PR:", inspect the
  PR review comments, determine which comments are valid and actionable, fix
  the valid issues, validate the changes, and resolve the addressed comments
  only after validation passes.

## Quick Commands

```bash
cd /Users/arunbabuchandrababu/Desktop/AIPROJECTS/HawksOptions
python3 -m unittest discover -v
python3 scheduler/run_scan.py --dry-run
python3 scheduler/run_risk_check.py --dry-run
python3 scheduler/run_backtest.py --days 30 --fund 10000
```

## Task Tracking

This repo supports `bd` (Beads) for repo-local task tracking.

- At the start of every agent session, run `bd ready --json` from the repo
  root before making changes.
- If `bd ready --json` fails because Beads is not initialized, run
  `./scripts/init_beads.sh`, then rerun `bd ready --json`.
- Claim matching ready work with `bd update <id> --claim --json` before
  implementation. If no matching task exists and the work is not trivial,
  create one with `bd create "<title>" -t task -p 2 --json`.
- Record discovered follow-up work in Beads instead of markdown task lists.
- Beads does not relax any trading or dashboard safety rules in this file.
- Use JSON output for automation: `bd ready --json`, `bd show <id> --json`,
  `bd update <id> --claim --json`.

Bootstrap once per checkout:

```bash
./scripts/init_beads.sh
bd ready --json
bd update <id> --claim --json
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

- focused unit tests for the functional behavior being added or changed
- the full unit-test suite passing
- lint checks passing
- deterministic backtest validation

```bash
python3 -m unittest discover -v
python3 -W error::DeprecationWarning -m unittest discover
ruff check .
python3 -m compileall core strategies scheduler ai tests dashboard scripts
python3 scheduler/run_backtest.py --days 30 --fund 10000
```

If any check fails, fix the issue before handing work off.
