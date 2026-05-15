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

## GitHub Research Implementation Plan

Generated from a May 15, 2026 scan of active/starred open-source options
automation and backtesting repos, including:

- `QuantConnect/Lean`
- `Lumiwealth/lumibot`
- `goldspanlabs/optopsy`
- `lambdaclass/options_portfolio_backtester`
- `marketcalls/openalgo`
- `AsyncAlgoTrading/aat`
- `sirnfs/OptionSuite`
- `cutemarkets/cutebacktests`
- focused options bot repos for Schwab, IBKR, earnings calendars, and option flow

The goal is not to copy code. The goal is to selectively bring proven design
patterns into HawksOptions while preserving existing safety rules:

- no naked short calls
- no uncovered short puts
- no entries below 7 DTE
- every order must pass `core.risk_manager.pre_trade_check`
- dashboard remains read-only
- `mode: paper` stays unchanged unless explicitly approved

### Phase 1 - Backtest Market Fidelity

- [ ] Add a real historical option-chain replay adapter.
  Source inspiration: `options_portfolio_backtester`, `optopsy`, `lumibot`.
  Required behavior: load daily or intraday option chains with bid/ask, volume,
  open interest, Greeks, expirations, delistings, corporate actions, stock
  prices, dividends, and earnings dates.

- [ ] Add data-source interfaces for CSV/Parquet and provider-backed data.
  Source inspiration: `lumibot` multi-provider routing and
  `options_portfolio_backtester` processed data loaders.
  Keep provider-specific code behind an interface so tests can run offline.

- [ ] Track contract-level lifecycle state in backtests.
  Source inspiration: `options_portfolio_backtester`.
  Required behavior: listed contract universe by date, expirations, assignment
  events, expired OTM options, ITM exercise assumptions, stock inventory, and
  cash/stock/options portfolio accounting.

- [ ] Record backtest data provenance in every report.
  Include data source, date range, symbols, option-chain coverage, missing-data
  rate, fill model, slippage model, commission model, and config hash.

### Phase 2 - Strategy Coverage

- [ ] Add broken-wing butterfly strategy.
  Safety rule: defined-risk only.
  Initial use case: neutral-to-directional high-IV setups where credit quality
  and max loss are explicit.

- [ ] Add long/short butterfly strategy templates.
  Safety rule: only allow fully hedged, defined-risk combinations.
  Include delta-targeted strike selection and explicit width constraints.

- [ ] Add collar strategy for long-stock inventory.
  Safety rule: requires existing long shares; no uncovered calls.
  Use case: protect assigned wheel shares or existing holdings during elevated
  event risk.

- [ ] Add diagonal spread strategy.
  Safety rule: front short leg must be covered by longer-dated option and pass
  assignment/ex-dividend checks.
  Use case: lower-IV or directional premium capture with calendar-like risk
  controls.

- [ ] Add tail-risk hedge strategy.
  Source inspiration: `options_portfolio_backtester` tail-risk hedge analysis.
  Use case: systematic small long-put hedge triggered by portfolio drawdown,
  volatility regime, or event risk.

- [ ] Add earnings calendar-spread candidate scanner.
  Source inspiration: focused earnings calendar-spread bot repos.
  Required filters: earnings date confidence, IV rank, front/back IV spread,
  liquidity, ex-dividend proximity, max debit, max assignment risk, and planned
  exit before/after event.

- [ ] Add volatility-crush earnings iron-condor research mode.
  Keep disabled by default.
  Required filters: high IV rank, liquid chains, expected move, minimum credit,
  max loss, no binary event over-sizing, and forced post-event exit.

### Phase 3 - Entry Filters And Thresholds

- [ ] Add IV percentile alongside IV rank.
  Use both as configurable gates because IV rank can be distorted by a single
  extreme observation.

- [ ] Add realized-vs-implied volatility spread filter.
  Use case: short-premium strategies should prefer implied volatility above
  realized volatility by a configurable margin.

- [ ] Add skew and term-structure filters.
  Source inspiration: `optopsy`, `openalgo` volatility surface tooling.
  Use case: avoid selling cheap tails; prefer structures where skew supports
  the selected strategy.

- [ ] Add OI profile and max-pain analytics as optional context.
  Source inspiration: `openalgo`.
  Use as scoring inputs only at first; do not let these bypass risk gates.

- [ ] Add gamma exposure / dealer positioning placeholder interface.
  Source inspiration: `openalgo` GEX dashboard.
  Use as optional market-regime context once reliable data is available.

- [ ] Add technical regime filters.
  Source inspiration: `optopsy` entry signals and general algo frameworks.
  Candidate inputs: RSI, MACD, Bollinger Bands, EMA trend, ATR expansion,
  realized volatility trend, and gap risk.

- [ ] Add event-risk filters.
  Required inputs: earnings, ex-dividend, FOMC/CPI/PPI/jobs dates, major
  index rebalance dates, and symbol-specific news vetoes.

### Phase 4 - Candidate Scoring And Portfolio Construction

- [ ] Expand strategy scoring beyond current simple score.
  Candidate inputs: credit-to-width, reward/risk, expected theta, max gamma,
  vega exposure, bid/ask width, volume, open interest, DTE fit, IV edge,
  event proximity, and portfolio Greek impact.

- [ ] Persist the full candidate set for every scan.
  Source inspiration: `lumibot` traceability and `openalgo` dashboards.
  Store generated candidates, rejected candidates, risk reasons, score inputs,
  chosen order, config hash, market snapshot, and data timestamp.

- [ ] Add portfolio allocation weights by strategy family.
  Source inspiration: `optopsy` weighted portfolio simulation.
  Example caps: max short-premium allocation, max long-premium allocation,
  max earnings-event allocation, max single-underlying allocation.

- [ ] Add portfolio Greek ceilings.
  Initial gates: max absolute delta, max negative gamma, max vega, minimum
  theta quality, and per-underlying Greek concentration.

- [ ] Add correlation and sector concentration caps.
  Avoid stacking SPY/QQQ/AAPL/MSFT risk as if they were independent.

- [ ] Add drawdown-aware risk throttling.
  Use case: reduce new entries after portfolio drawdown or daily loss even if
  hard stop has not triggered.

### Phase 5 - Execution And Fill Realism

- [ ] Add configurable fill models.
  Source inspiration: `optopsy` slippage models.
  Models: mid fill, bid/ask pessimistic fill, half-spread, liquidity-based,
  per-leg slippage, and failed-fill probability.

- [ ] Add multi-leg execution quality tracking.
  Log expected credit/debit, actual fill, slippage by leg, order duration,
  partial fill handling, and retry count.

- [ ] Add limit-price improvement rules.
  Use case: start near mid, then widen toward max acceptable debit/credit
  without violating minimum credit-to-width or max loss.

- [ ] Add stale quote protection.
  Reject orders when quote timestamp, bid/ask spread, or market data freshness
  fails configured thresholds.

- [ ] Add broker adapter abstraction tests.
  Source inspiration: Lean, Lumibot, OpenAlgo.
  Ensure Alpaca-specific code is isolated from strategy, risk, and backtest
  logic.

### Phase 6 - Observability And Reporting

- [ ] Add richer backtest metrics.
  Source inspiration: `optopsy`.
  Metrics: Sortino, Calmar, VaR, CVaR, Omega, tail ratio, profit factor,
  expectancy, average win/loss, max consecutive losses, exposure time, and
  return by strategy.

- [ ] Add rejected-reason dashboards and reports.
  Use case: identify whether risk caps, liquidity, IV gates, DTE, earnings,
  or scoring thresholds are blocking most candidates.

- [ ] Add strategy attribution reports.
  Break down PnL, drawdown, win rate, average hold time, slippage, and risk
  usage by strategy and symbol.

- [ ] Add scan health report.
  Include symbols scanned, chain availability, stale data, missing Greeks,
  candidate count, accepted count, rejected count, and top rejection reasons.

- [ ] Add paper-vs-backtest drift report.
  Compare expected fills, slippage, hold times, exits, and PnL between backtest
  and paper trading.

- [ ] Add read-only dashboard panels for new analytics.
  Preserve dashboard safety rule: no mutation endpoints.
  Candidate panels: IV/skew, portfolio Greeks, candidate funnel, risk budget,
  drawdown throttle state, strategy attribution, and rejected-reason trends.

### Phase 7 - AI And Human Review Safety

- [ ] Keep AI veto-only for trading decisions.
  AI may summarize candidate context, explain risk, or veto on major concerns.
  AI must not originate orders or bypass deterministic gates.

- [ ] Add read-only research agent traces.
  Source inspiration: Lumibot AI traceability.
  Store prompt inputs, tool outputs, recommendation summary, and veto reason.

- [ ] Add deterministic pre-AI feature packet.
  Ensure the AI sees the same bounded candidate summary for every strategy:
  Greeks, liquidity, IV, event dates, scoring inputs, risk usage, and exits.

- [ ] Add AI disagreement logging.
  Track when deterministic gates accept but AI vetoes, or deterministic gates
  reject before AI. Use this for review only, not auto-relaxation.

### Phase 8 - Implementation Order

- [ ] Implement market-data replay improvements before adding more strategies.
  Rationale: strategy tuning is low-confidence until market fidelity improves.

- [ ] Implement richer metrics and candidate logging before optimizing
  thresholds.
  Rationale: threshold tuning needs observability and anti-overfit reporting.

- [ ] Add one new strategy family at a time.
  Suggested order: collar, broken-wing butterfly, diagonal/calendar upgrade,
  tail hedge, then earnings-specific strategies.

- [ ] Keep all new strategies disabled by default until paper validated.
  Required validation: focused unit tests, full test suite, lint, compile,
  deterministic backtest, fixture replay backtest, and paper-trading dry run.

- [ ] Convert durable follow-up items into Beads tasks when implementation
  begins.
  This `todo.md` is a research plan; active work should use `bd` where
  practical per repo instructions.
