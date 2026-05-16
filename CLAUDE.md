# HawksOptions — Operating Manual

## Non-Negotiables

1. Defined-risk strategies only.
2. Portfolio risk is measured by maximum loss, not notional exposure.
3. Short calls near ex-dividend must be closed when dividend exceeds remaining
   extrinsic value.
4. No same-day expiry entries.
5. AI is veto-only and optional.

## Persistent User Learnings

When the user says "Learning for you", update the relevant Markdown guidance
files with the instruction that follows. Do not leave these workflow learnings
only in chat context.

When the user provides a pull request link prefixed with "PR:", inspect the PR
review comments, fix valid actionable comments, validate the changes, and
resolve the comments only after everything is clean.

## Daily Operating Schedule

- `run_scan.py`: every 30 minutes
- `run_risk_check.py`: every 5 minutes baseline
- `run_risk_watch.py`: every 1 minute for elevated positions
- `run_roll_check.py`: hourly
- `run_eod_report.py`: after market close

## Emergency Stop

```bash
sudo systemctl stop 'hawksoptions-*.timer'
```

## Validation After Every Change

Functional changes must include focused unit tests before validation.

```bash
python3 -m unittest discover -v
python3 -W error::DeprecationWarning -m unittest discover
ruff check .
python3 -m compileall core strategies scheduler ai tests dashboard scripts
python3 scheduler/run_backtest.py --days 30 --fund 10000
```

## Beads Task Tracking

This repo supports `bd` (Beads) for local task tracking. Every agent session
must start with `bd ready --json` from the repo root. If Beads is not
initialized, run `./scripts/init_beads.sh`, then rerun `bd ready --json`.

Claim matching ready work with `bd update <id> --claim --json` before making
changes. For non-trivial untracked work, create a task with `bd create "<title>"
-t task -p 2 --json`. Record follow-up work in Beads instead of markdown task
lists.

Bootstrap once with:

```bash
./scripts/init_beads.sh
bd ready --json
bd update <id> --claim --json
```

## Deployment Notes

- Keep trading and dashboard credentials separate.
- The dashboard never reads trading secrets from `/dev/shm`.
- Default mode stays `paper` until a human changes it deliberately.
