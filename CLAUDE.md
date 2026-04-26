# HawksOptions — Operating Manual

## Non-Negotiables

1. Defined-risk strategies only.
2. Portfolio risk is measured by maximum loss, not notional exposure.
3. Short calls near ex-dividend must be closed when dividend exceeds remaining
   extrinsic value.
4. No same-day expiry entries.
5. AI is veto-only and optional.

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

This repo supports `bd` (Beads) for local task tracking. If it is initialized
for the checkout, prefer `bd ready --json` and `bd update <id> --claim --json`
over markdown task lists.

Bootstrap once with:

```bash
./scripts/init_beads.sh
```

## Deployment Notes

- Keep trading and dashboard credentials separate.
- The dashboard never reads trading secrets from `/dev/shm`.
- Default mode stays `paper` until a human changes it deliberately.
