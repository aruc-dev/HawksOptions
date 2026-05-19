# Production Go-Live Gate

Do not set `mode: live` until this checklist is complete.

- Reconciler is clean for 14 consecutive paper trading days.
- Zero P1 alerts for 14 consecutive paper trading days.
- Real-data 12-month backtest has positive expectancy, profit factor at least 1.3, max drawdown at most 8%, and walk-forward-selected parameters.
- Out-of-sample 3-month validation is within 25% of in-sample expectancy.
- Daily audit pack reproduces the broker statement for a sampled paper day.
- Kill switch has been tested in the last 7 days.
- Backup restore has been tested in the last 30 days.
- Every P1 alert has a runbook.
- Risk-critical paths are protected by CODEOWNERS and required CI.

Capital ramp is calendar-based, not a config flip:

- Week 1: $1k notional cap, max 1 open strategy, daily operator review.
- Weeks 2-3: $5k notional cap, max 2 open strategies, daily operator review.
- Weeks 4-6: $25k notional cap, max 4 open strategies, every-other-day review.
- Week 7+: graduate only if prior tranches stayed within expected PnL distribution and had zero system-caused P1 alerts.
