# Runbook: Kill Switch Tripped

Severity: P1

Trigger: `data/HALTED` exists or the dashboard reports `hawksoptions_halted`.

## Check

- Read `data/HALTED` for the halt reason.
- Confirm no scheduler job is actively submitting orders.
- Run `python3 scheduler/run_reconcile.py` after broker access is confirmed.

## Act

- Do not remove the halt file until reconciliation is clean.
- If positions differ from the broker, manage the broker account directly and document the manual action in the daily audit pack notes.
- After remediation, remove `data/HALTED` only during market-safe hours and run `python3 scheduler/run_scan.py --dry-run`.

## Escalate

Escalate immediately if the halt reason starts with `reconciliation_`, `daily_loss`, `broker_outage`, or `fresh_nbbo_required`.
