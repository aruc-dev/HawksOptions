# HawksOptions

![HawksOptions Brand](assets/brand/hawksoptions-brand.png)

HawksOptions is a separate options-native trading system built from
`options_trader_spec.md`. It keeps the operational shape of `HawksTrade`
without sharing runtime state or mutating the reference repo.

## What It Does

- Trades defined-risk options structures only
- Enforces portfolio max-loss limits before every order
- Tracks IV rank, Greeks, earnings blackout windows, and ex-dividend risk
- Ships with a read-only FastAPI dashboard and Linux/systemd deployment assets
- Paper scans use live Alpaca market data by default, with a deterministic
  sample-data mode available for local tests and the bundled backtest

## Repository Layout

```text
HawksOptions/
├── assets/                  # branding and visual assets
├── config/                  # config.yaml, config.local.yaml (optional local overlay, git-ignored), .env.example, underlyings.yaml
├── core/                    # options primitives, risk engine, trade logging
├── strategies/              # CSP, covered call, vertical spread, iron condor
├── ai/                      # veto-only AI helpers (optional)
├── scheduler/               # scan, risk checks, roll checks, backtests
├── dashboard/               # read-only FastAPI dashboard
├── cloud-setup/             # EC2/systemd/dashboard deployment guides
├── scripts/                 # health checks, secrets, systemd diagnostics
└── tests/                   # unittest coverage for core scheduler logic
```

## Quick Start

```bash
cd /Users/arunbabuchandrababu/Desktop/AIPROJECTS/HawksOptions
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pip install -r requirements-dashboard.txt
cp config/.env.example config/.env
python3 -m unittest discover -v
python3 scheduler/run_backtest.py --days 30 --fund 10000
python3 scheduler/run_scan.py --dry-run
```

The committed paper config uses live Alpaca market data. For local deterministic
dry-runs without credentials, create `config/config.local.yaml` with:

```yaml
market_data:
  use_sample_data: true
```

## Local Configuration

All committed defaults live in `config/config.yaml`. For machine-local changes,
create `config/config.local.yaml`; it is deep-merged over `config/config.yaml`,
so it only needs the keys you want to override. Files matching
`config/config.local*` are git-ignored and must not be committed.

When `gates.vix_iv_rank_scaling.enabled` is turned on with live market data,
`market_data.vix_symbol` defaults to `VIXY`, a tradable Alpaca stock/ETF proxy.
Override it in `config/config.local.yaml` if your provider uses a different
volatility proxy. Because proxy ETF prices are not on the same scale as the VIX
index, live proxy mode fails closed until `proxy_low_below` or
`proxy_high_above` is configured under `gates.vix_iv_rank_scaling`.

## Optional: Beads Task Tracking

This repo includes repo-level support for `bd` (Beads), an agent-oriented task
tracker. Agent sessions should run `bd ready --json` before making changes. If
the checkout is not initialized, run `./scripts/init_beads.sh`, then rerun
`bd ready --json`.

Use Beads for non-trivial tracked work:

```bash
bd ready --json
bd update <id> --claim --json
bd create "Title" -t task -p 2 --json
```

See [docs/beads.md](docs/beads.md) for setup and workflow details.

## Safety Defaults

- `mode: paper`
- defined-risk strategies only
- live Alpaca market data for paper scans
- no 0-DTE or 1-DTE entries
- max single-position risk capped at 5% of equity
- max portfolio defined-risk capped at 20% of equity
- earnings blackout and ex-dividend protection enabled

Cash-secured puts and covered calls are disabled in the committed paper profile.
One-lot cash-secured puts on default symbols can exceed the 5% single-position
risk cap on a $100k paper account, and covered calls require existing long stock
inventory. Re-enable either only through a local config overlay after reviewing
the risk impact.

## Backtest Disclaimer

Rule fidelity is decent. Market fidelity is not.

The bundled backtest is reasonably close in the sense that it uses the repo's
actual strategy constructors, the same pre-trade risk gates, and the same
continuous exit rules. That makes it useful for regression testing and comparing
one config against another.

It is not close enough to treat the PnL as live-trading expectancy. The default
backtest uses deterministic sample market data and synthetic option chains, so
results should be treated as a strategy-logic signal rather than a real-world
profit forecast.

## Dashboard

The dashboard remains read-only. It exposes:

- account and buying-power summary
- daily-loss headroom
- open strategy aggregates
- portfolio Greeks
- IV rank heatmap
- earnings calendar and health state

See [cloud-setup/dashboard-setup.md](/Users/arunbabuchandrababu/Desktop/AIPROJECTS/HawksOptions/cloud-setup/dashboard-setup.md)
for the deployment pattern.

## Validation

Before publishing or deploying:

- add focused unit tests for every functional change
- run the full unit-test suite
- run lint checks
- validate the change with the deterministic backtest

```bash
python3 -m unittest discover -v
python3 -W error::DeprecationWarning -m unittest discover
ruff check .
python3 -m compileall core strategies scheduler ai tests dashboard scripts
python3 scheduler/run_backtest.py --days 30 --fund 10000
python3 scheduler/run_scan.py --dry-run
python3 scheduler/run_risk_check.py --dry-run
python3 scheduler/run_eod_report.py --dry-run
```

## Status

This repo implements the phase-1 through phase-3 architecture from the spec,
with optional AI components left veto-only and disabled by default.
