# HawksOptions Strategy TODOs

Generated from a repo review of the implemented strategies, config gates,
backtest engine, and the referenced automation guidance around defined-risk
options strategies.

Current baseline check:

- Command: `python3 scheduler/run_backtest.py --days 30 --fund 10000`
- Result: ending equity `$9,956.60`, return `-0.43%`, Sharpe `-3.6459`,
  max drawdown `0.43%`, win rate `0.00%`, closed trades `2`
- Interpretation: useful as a rule-regression signal only. The current
  backtest forces sample data and synthetic option chains, so it is not a
  live-expectancy estimate.

## High Priority

- [x] Add a historical market-data backtest adapter before treating tuning
  results as profit guidance. `core/backtest_engine.py` currently instantiates
  `AlpacaOptionsClient(config, use_sample_data=True)`, so every run uses the
  deterministic sample chain instead of real historical option quotes, open
  interest, volume, corporate actions, assignment events, and changing listed
  expirations/strikes.
  Implemented with a fixture-backed replay client selected by
  `backtest.data_source: fixture`.

- [x] Implement dynamic strategy scoring instead of fixed first-accepted
  strategy selection. `strategies/__init__.py` defines a fixed registry order
  and both `scheduler/run_scan.py` and `core/backtest_engine.py` stop after the
  first accepted strategy per underlying. The `weight` values in
  `config/config.yaml` are currently not used in selection, so iron condors,
  credit spreads, CSPs, and future strategies cannot be ranked by regime,
  credit quality, IV rank, or expected risk-adjusted return.
  Implemented with accepted-candidate scoring in scan and backtest paths.

- [x] Enforce configured per-strategy and per-underlying contract limits.
  `config/config.yaml` includes `max_contracts_per_underlying` and
  `config/underlyings.yaml` includes `max_contracts`, but generated strategy
  quantities are effectively fixed at one contract except covered calls. Wire
  these caps into sizing before `core.risk_manager.pre_trade_check`.
  Implemented in shared strategy sizing helpers before pre-trade checks.

- [x] Add minimum premium-quality gates for credit spreads and iron condors.
  `strategies/vertical_spread.py` and `strategies/iron_condor.py` only reject
  non-positive credit or impossible max-loss math. Add configurable checks such
  as minimum credit-to-width, minimum net credit after modeled slippage, and
  minimum reward-to-risk before an order reaches the risk manager.
  Implemented as configurable credit-quality gates on spread/condor strategies.

- [x] Pass current implied volatility through `StrategyContext`. The iron
  condor regime filter compares realized volatility to
  `context.underlying.get("current_iv", context.iv_rank / 100.0)`, but
  `scheduler/common.py` passes the static underlying config into context
  without the snapshot's `current_iv`. This can make IV-rank behave as a
  volatility proxy and distort condor eligibility.
  Implemented with `StrategyContext.current_iv` in scan and backtest context.

## Strategy Additions Or Tuning

- [x] Add a wheel state machine only if share inventory and assignment
  simulation are implemented first. The repo has cash-secured puts and covered
  calls, but `scheduler/common.py` currently sets `long_shares=0` and
  `cost_basis=0.0`, so covered-call automation is not connected to actual
  stock inventory. A safe wheel implementation should model cash-secured put
  assignment, share ownership, covered-call assignment, cost basis, and cash
  coverage without introducing naked short calls or uncovered puts.
  Implemented the required inventory and assignment hooks; no separate unsafe
  wheel strategy was enabled.

- [x] Split vertical-spread selection into bullish and bearish candidates, or
  add a regime selector. `VerticalSpreadStrategy` supports `bull_put_credit`
  and `bear_call_credit`, but the config exposes one `vertical_spread` variant
  at a time. A practical automated spread system should evaluate trend/regime
  inputs and choose bull put, bear call, or no trade.
  Implemented `variant: auto` support using configured trend metadata.

- [x] Keep `calendar_spread` disabled until its selection logic uses the
  configured front/back expirations and validates assignment exposure. The
  current implementation finds a target front call, then chooses the earliest
  and latest same-strike expirations available after filtering, which may not
  match `front_dte` and `back_dte` intent.
  Implemented back-leg selection against configured `back_dte`; the strategy
  remains disabled by default.

- [x] Keep `earnings_iron_condor` disabled until earnings data and event-vol
  modeling are dynamic. The strategy requires the earnings date to be exactly
  tomorrow and has hard-coded short/long deltas plus profit/stop settings. It
  should be paper-tested with real event calendars and historical implied-vol
  crush behavior before enablement.
  Implemented configurable event timing and deltas; the strategy remains
  disabled by default pending paper validation with live event data.

- [x] Tune iron-condor IV and range-bound gates after historical replay exists.
  The current config uses `min_iv_rank_for_short_premium: 30` globally and
  condor-specific delta/DTE settings. The external guidance points toward
  higher-IV, range-bound deployment; test IV-rank thresholds, realized-vs-IV
  filters, ATR limits, deltas, and profit-take settings with walk-forward
  validation rather than a single synthetic sample run.
  Implemented configurable iron-condor IV/range gates and walk-forward tuning
  support. Live-expectancy tuning still requires real historical data input.

## Backtest And Test Coverage

- [x] Extend the tuning harness to run parameter grids and report return,
  Sharpe, drawdown, trade count, win rate, and rejected-trade reasons across
  train/test windows. `scheduler/run_tuning.py` supports one-off overrides but
  does not yet provide walk-forward or anti-overfit reporting.
  Implemented `--grid`, `--walk-forward`, `--start-date`, and rejected-reason
  reporting.

- [x] Add tests proving strategy `weight`, `max_contracts`, `current_iv`,
  credit-quality gates, and wheel inventory state are enforced once those
  features are implemented.
  Implemented focused coverage in `tests/test_strategy_todos.py`.

- [x] Add backtest fixtures that replay a dynamic historical option universe.
  The current synthetic chain is dynamic by date, but it is not a historical
  listed-contract universe with real bid/ask changes, volume, open interest,
  expirations, delistings, assignments, or corporate-event effects.
  Implemented fixture replay files under `tests/fixtures/`.
