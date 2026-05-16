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

- [x] Add a real historical option-chain replay adapter.
  Source inspiration: `options_portfolio_backtester`, `optopsy`, `lumibot`.
  Required behavior: load daily or intraday option chains with bid/ask, volume,
  open interest, Greeks, expirations, delistings, corporate actions, stock
  prices, dividends, and earnings dates.
  Implemented `HistoricalReplayClient` with JSON replay support for provider
  metadata, dated snapshots, option chains, listed/delisted contract filtering,
  earnings dates, dividends, corporate actions, and coverage summaries. The
  existing fixture mode remains backwards-compatible.

- [x] Add data-source interfaces for CSV/Parquet and provider-backed data.
  Source inspiration: `lumibot` multi-provider routing and
  `options_portfolio_backtester` processed data loaders.
  Keep provider-specific code behind an interface so tests can run offline.
  Implemented `HistoricalDataSource` boundaries for JSON, CSV, optional Parquet,
  and provider-backed data. CSV/Parquet normalize into the same replay payload;
  provider mode is explicit and isolated until a concrete vendor adapter is
  implemented.

- [x] Track contract-level lifecycle state in backtests.
  Source inspiration: `options_portfolio_backtester`.
  Required behavior: listed contract universe by date, expirations, assignment
  events, expired OTM options, ITM exercise assumptions, stock inventory, and
  cash/stock/options portfolio accounting.
  Implemented lifecycle-aware mark-to-market for missing expired contracts:
  expired OTM contracts value to zero, expired ITM contracts value to intrinsic
  using the replay snapshot, and non-expired missing contracts are tagged as
  stale quote fallbacks. Existing assignment inventory accounting now consumes
  these refreshed expired contract values.

- [x] Record backtest data provenance in every report.
  Include data source, date range, symbols, option-chain coverage, missing-data
  rate, fill model, slippage model, commission model, and config hash.
  Implemented `BacktestResult.provenance` plus a markdown `Data Provenance`
  section containing data source/format, date range, symbols, replay coverage,
  fill/slippage/commission models, and a stable config hash.

### Phase 2 - Strategy Coverage

- [x] Add broken-wing butterfly strategy.
  Safety rule: defined-risk only.
  Initial use case: neutral-to-directional high-IV setups where credit quality
  and max loss are explicit.
  Implemented disabled-by-default `BrokenWingButterflyStrategy` with 1:-2:1
  defined-risk legs, conservative max-loss/buying-power calculation, credit
  quality gates, strategy registry/type wiring, and pre-trade risk coverage.

- [x] Add long/short butterfly strategy templates.
  Safety rule: only allow fully hedged, defined-risk combinations.
  Include delta-targeted strike selection and explicit width constraints.
  Implemented disabled-by-default `ButterflyStrategy` with long-debit and
  short-credit 1:-2:1 templates, delta-targeted body selection, symmetric wing
  width enforcement, explicit max loss/profit, and pre-trade risk coverage.

- [x] Add collar strategy for long-stock inventory.
  Safety rule: requires existing long shares; no uncovered calls.
  Use case: protect assigned wheel shares or existing holdings during elevated
  event risk.
  Implemented disabled-by-default `CollarStrategy` for existing long-stock
  inventory with whole-lot coverage enforcement, protective put / covered call
  selection, cost-basis-aware max loss/profit, earnings blackout gating, and
  pre-trade risk coverage.

- [x] Add diagonal spread strategy.
  Safety rule: front short leg must be covered by longer-dated option and pass
  assignment/ex-dividend checks.
  Use case: lower-IV or directional premium capture with calendar-like risk
  controls.
  Implemented disabled-by-default `DiagonalSpreadStrategy` with call/put debit
  variants, longer-dated covering-leg enforcement, ex-dividend entry blocking
  for short calls, front-leg assignment-risk monitoring, and pre-trade risk
  coverage.

- [x] Add tail-risk hedge strategy.
  Source inspiration: `options_portfolio_backtester` tail-risk hedge analysis.
  Use case: systematic small long-put hedge triggered by portfolio drawdown,
  volatility regime, or event risk.
  Implemented disabled-by-default `TailRiskHedgeStrategy` using budget-capped
  long puts, drawdown/daily-loss/ATR/IV-over-realized/event-risk triggers,
  earnings blackout gating, and pre-trade risk coverage.

- [x] Add earnings calendar-spread candidate scanner.
  Source inspiration: focused earnings calendar-spread bot repos.
  Required filters: earnings date confidence, IV rank, front/back IV spread,
  liquidity, ex-dividend proximity, max debit, max assignment risk, and planned
  exit before/after event.
  Implemented read-only research scanner output for earnings calendar spreads
  with confidence/timing, IV-rank, front/back-IV-spread, liquidity,
  ex-dividend, assignment-risk, max-debit, and planned-exit filters.

- [x] Add volatility-crush earnings iron-condor research mode.
  Keep disabled by default.
  Required filters: high IV rank, liquid chains, expected move, minimum credit,
  max loss, no binary event over-sizing, and forced post-event exit.
  Implemented disabled-by-default read-only volatility-crush iron-condor
  scanner with high-IV, liquidity, expected-move, credit, max-loss,
  event-risk-sizing, ex-dividend, and forced-exit filters.

### Phase 3 - Entry Filters And Thresholds

- [x] Add IV percentile alongside IV rank.
  Implemented `compute_iv_percentile`, sample/replay snapshot propagation,
  scheduler/backtest `StrategyContext` wiring, and regression coverage for
  sample and historical replay snapshots.
  Use both as configurable gates because IV rank can be distorted by a single
  extreme observation.

- [x] Add realized-vs-implied volatility spread filter.
  Implemented reusable implied-vs-realized spread/ratio gates in
  `BaseStrategy`, applied them to short-premium strategy constructors, exposed
  spread metadata in candidate selection, and added regression coverage.
  Use case: short-premium strategies should prefer implied volatility above
  realized volatility by a configurable margin.

- [x] Add skew and term-structure filters.
  Source inspiration: `optopsy`, `openalgo` volatility surface tooling.
  Use case: avoid selling cheap tails; prefer structures where skew supports
  the selected strategy.
  Implemented reusable volatility-surface metrics for put/call tail skew and
  term-structure slope, non-restrictive default gates for short-premium
  strategies, selection metadata, and regression coverage for skew and term
  filters.

- [x] Add OI profile and max-pain analytics as optional context.
  Source inspiration: `openalgo`.
  Use as scoring inputs only at first; do not let these bypass risk gates.
  Implemented open-interest profile, max-pain strike, max-pain distance, and
  largest-OI context as scoring metadata with a small ranking component only;
  added coverage for normal and zero-effective-OI chains.

- [x] Add gamma exposure / dealer positioning placeholder interface.
  Source inspiration: `openalgo` GEX dashboard.
  Use as optional market-regime context once reliable data is available.
  Implemented `DealerPositioningProvider`/snapshot placeholders, metadata
  parsing from underlyings, selection metadata plumbing, and regression coverage
  proving dealer context does not change selection score or risk acceptance.

- [x] Add technical regime filters.
  Implemented optional trend, RSI, and price-vs-SMA regime gates with
  scheduler/backtest context propagation, non-restrictive defaults, selection
  metadata, and regression coverage for configured pass/fail behavior.
  Source inspiration: `optopsy` entry signals and general algo frameworks.
  Candidate inputs: RSI, MACD, Bollinger Bands, EMA trend, ATR expansion,
  realized volatility trend, and gap risk.

- [x] Add event-risk filters.
  Required inputs: earnings, ex-dividend, FOMC/CPI/PPI/jobs dates, major
  index rebalance dates, and symbol-specific news vetoes.
  Implemented optional explicit event-risk gates (`block_event_risk` and
  `max_event_risk_level`) across strategy constructors, selection metadata,
  and regression coverage proving default no-op behavior plus configured
  blocking.

### Phase 4 - Candidate Scoring And Portfolio Construction

- [x] Expand strategy scoring beyond current simple score.
  Candidate inputs: credit-to-width, reward/risk, expected theta, max gamma,
  vega exposure, bid/ask width, volume, open interest, DTE fit, IV edge,
  event proximity, and portfolio Greek impact.
  Implemented transparent score components and configurable weights for
  credit-to-width, theta efficiency, liquidity, DTE fit, event proximity,
  gamma/vega safety, and portfolio Greek room. These remain ranking-only and
  cannot bypass strategy constructors or pre-trade risk gates.

- [x] Persist the full candidate set for every scan.
  Source inspiration: `lumibot` traceability and `openalgo` dashboards.
  Store generated candidates, rejected candidates, risk reasons, score inputs,
  chosen order, config hash, market snapshot, and data timestamp.
  Implemented per-scan JSON traces under `reports/candidate_scans/` with
  generated/ranked candidates, chosen orders, accepted/rejected decisions,
  research candidates, score metadata, config hash, market snapshot, and scan
  timestamp. The scan result now returns `candidate_report_path`.

- [x] Add portfolio allocation weights by strategy family.
  Source inspiration: `optopsy` weighted portfolio simulation.
  Example caps: max short-premium allocation, max long-premium allocation,
  max earnings-event allocation, max single-underlying allocation.
  Implemented optional `portfolio_allocation` pre-trade caps for strategy
  families and single-underlying exposure using existing max-loss exposure.
  Defaults remain non-restrictive unless caps are supplied in config, and
  malformed/negative cap values are ignored.

- [x] Add portfolio Greek ceilings.
  Implemented optional projected portfolio Greek limits for delta, theta, vega,
  and gamma in `pre_trade_check`, reusing the same sign convention as portfolio
  Greek reporting. Limits can come from config or account metadata and remain
  non-restrictive by default.
  Initial gates: max absolute delta, max negative gamma, max vega, minimum
  theta quality, and per-underlying Greek concentration.

- [x] Add correlation and sector concentration caps.
  Implemented optional pre-trade caps for sector and correlation-group
  concentration using configured underlying metadata. Scan and backtest paths
  provide metadata to risk checks without mutating caller config, and cap lookup
  supports case-insensitive specific group keys.
  Avoid stacking SPY/QQQ/AAPL/MSFT risk as if they were independent.

- [x] Add drawdown-aware risk throttling.
  Implemented optional `risk_throttle` pre-trade gates for drawdown halts,
  daily-loss halts, and reduced max position risk after configurable drawdown
  thresholds. Defaults remain no-op unless throttle settings and account
  drawdown/daily-loss metadata are supplied.
  Use case: reduce new entries after portfolio drawdown or daily loss even if
  hard stop has not triggered.

### Phase 5 - Execution And Fill Realism

- [x] Add configurable fill models.
  Source inspiration: `optopsy` slippage models.
  Models: mid fill, bid/ask pessimistic fill, half-spread, liquidity-based,
  per-leg slippage, and failed-fill probability.
  Implemented backtest fill-model selection with `mid`, `half_spread` /
  `bid_ask`, and `liquidity_based` spread costs, existing per-leg slippage and
  commission settings, deterministic failed-fill probability, provenance
  reporting, and regression coverage.

- [x] Add multi-leg execution quality tracking.
  Log expected credit/debit, actual fill, slippage by leg, order duration,
  partial fill handling, and retry count.
  Implemented execution-quality summaries for dry-run/live order responses,
  per-leg expected/actual fill and slippage tracking, order duration, partial
  fill status, retry count, scan result metadata, and trade-log columns for
  execution-quality fields.

- [x] Add limit-price improvement rules.
  Use case: start near mid, then widen toward max acceptable debit/credit
  without violating minimum credit-to-width or max loss.
  Implemented a bounded package limit-price improvement plan that starts at
  net mid, widens toward the unfavorable bid/ask edge, preserves configured
  minimum credit quality for credit trades, caps debit widening by max-loss
  limits, fixes multi-leg payloads to use true net package pricing, and records
  the plan in execution metadata.

- [x] Add stale quote protection.
  Reject orders when quote timestamp, bid/ask spread, or market data freshness
  fails configured thresholds.
  Implemented optional pre-trade quote freshness gates for stale, missing,
  future, invalid, wide-spread, and stale-lifecycle-fallback quotes. Historical
  replay quote timestamps are consumed from contract metadata, sample-data
  contracts now include quote timestamps, and datetime `as_of` values work with
  earnings blackout checks.

- [x] Add broker adapter abstraction tests.
  Source inspiration: Lean, Lumibot, OpenAlgo.
  Ensure Alpaca-specific code is isolated from strategy, risk, and backtest
  logic.
  Implemented repo-owned broker/market-data Protocols, moved scheduler helper
  annotations to those boundaries, and added tests proving a fake non-Alpaca
  broker can submit through the executor while strategy, risk, backtest, and
  execution layers remain free of Alpaca adapter imports.

### Phase 6 - Observability And Reporting

- [x] Add richer backtest metrics.
  Source inspiration: `optopsy`.
  Metrics: Sortino, Calmar, VaR, CVaR, Omega, tail ratio, profit factor,
  expectancy, average win/loss, max consecutive losses, exposure time, and
  return by strategy.
  Implemented richer `BacktestResult.metrics` output with Sortino, Calmar,
  VaR/CVaR, Omega, tail ratio, profit factor, expectancy, average win/loss,
  max consecutive losses, exposure time, average hold time, PnL by strategy and
  symbol, percentage return by strategy, and strategy trade counts. Metrics are
  included in CLI JSON and markdown backtest reports.

- [x] Add rejected-reason dashboards and reports.
  Use case: identify whether risk caps, liquidity, IV gates, DTE, earnings,
  or scoring thresholds are blocking most candidates.
  Implemented scan rejection markdown reports under `reports/rejections/`,
  added `rejection_report_path` to scan output, and exposed latest rejection
  summaries through read-only dashboard data sources, `/api/rejections/summary`,
  and `/api/state`.

- [x] Add strategy attribution reports.
  Break down PnL, drawdown, win rate, average hold time, slippage, and risk
  usage by strategy and symbol.
  Implemented `BacktestResult.attribution` plus
  `reports/strategy_attribution_<days>d.md`, grouped by strategy and symbol
  with modeled PnL, realized drawdown, win rate, hold time, entry slippage,
  and risk usage. Backtest CLI JSON now includes the attribution payload and
  report path.

- [x] Add scan health report.
  Include symbols scanned, chain availability, stale data, missing Greeks,
  candidate count, accepted count, rejected count, and top rejection reasons.
  Implemented scan-health payloads in scan output and candidate scan JSON, plus
  markdown reports under `reports/scan_health/`. The report includes per-symbol
  chain availability, contract/expiration coverage, stale quote indicators,
  missing Greeks, invalid/wide quotes, funnel counts, and top rejection reasons.

- [x] Add paper-vs-backtest drift report.
  Compare expected fills, slippage, hold times, exits, and PnL between backtest
  and paper trading.
  Implemented `scheduler/run_drift_report.py` and `reports/drift/`
  markdown output. The report compares paper expected-vs-actual entry fields,
  logged slippage, order duration, hold time, exit reasons, and available PnL
  percentages against deterministic backtest trade count, modeled slippage,
  hold time, total return, and modeled PnL, with explicit limitations.

- [x] Add read-only dashboard panels for new analytics.
  Preserve dashboard safety rule: no mutation endpoints.
  Candidate panels: IV/skew, portfolio Greeks, candidate funnel, risk budget,
  drawdown throttle state, strategy attribution, and rejected-reason trends.
  Implemented read-only `/api/analytics` and `/api/state` analytics payloads
  for candidate funnel, scan health, risk budget, strategy attribution, and
  paper-vs-backtest drift. Added passive dashboard panels that render the
  analytics as JSON without adding any mutation endpoints.

### Phase 7 - AI And Human Review Safety

- [x] Keep AI veto-only for trading decisions.
  AI may summarize candidate context, explain risk, or veto on major concerns.
  AI must not originate orders or bypass deterministic gates.
  Added a centralized safe AI review result contract that strips any
  trade-instruction fields before AI output leaves the review boundary.
  Scan wiring now stores only sanitized severity, concerns, source, and reason;
  deterministic pre-trade gates remain the only path to order approval.

- [x] Add read-only research agent traces.
  Source inspiration: Lumibot AI traceability.
  Implemented read-only research trace JSON under `reports/research_traces/`
  for each scan, recording scanner name, symbol, enabled state, candidate
  count, and candidates. Traces are included in candidate scan JSON and exposed
  on the dashboard analytics payload without influencing order generation.
  Store prompt inputs, tool outputs, recommendation summary, and veto reason.

- [x] Add deterministic pre-AI feature packet.
  Ensure the AI sees the same bounded candidate summary for every strategy:
  Greeks, liquidity, IV, event dates, scoring inputs, risk usage, and exits.
  Implemented a schema-versioned pre-AI packet in the scan path containing the
  deterministic order summary, structural critique result, risk warnings, and
  candidate selection metadata. External AI review now receives this bounded
  packet instead of an ad hoc order summary.

- [x] Add AI disagreement logging.
  Track when deterministic gates accept but AI vetoes, or deterministic gates
  reject before AI. Use this for review only, not auto-relaxation.
  Implemented read-only disagreement JSON logs under
  `reports/ai_disagreements/`, with scan output and candidate-scan payloads
  recording deterministic reject-before-AI cases and AI veto-after-deterministic
  accept cases. Dashboard analytics now expose the latest log passively.

### Phase 8 - Implementation Order

- [x] Implement market-data replay improvements before adding more strategies.
  Rationale: strategy tuning is low-confidence until market fidelity improves.
  Completed before strategy expansion via `HistoricalReplayClient`,
  JSON/CSV/optional Parquet/provider data-source boundaries, contract lifecycle
  handling, event metadata, and provenance reporting in backtest results.

- [x] Implement richer metrics and candidate logging before optimizing
  thresholds.
  Rationale: threshold tuning needs observability and anti-overfit reporting.
  Completed with richer backtest metrics, strategy attribution reports,
  candidate-scan JSON, rejection reports, scan-health reports,
  paper-vs-backtest drift reports, pre-AI feature packets, and AI disagreement
  logs.

- [x] Add one new strategy family at a time.
  Suggested order: collar, broken-wing butterfly, diagonal/calendar upgrade,
  tail hedge, then earnings-specific strategies.
  Completed through isolated strategy implementations and focused tests for
  collar, broken-wing butterfly, butterfly, diagonal/calendar assignment-risk
  handling, tail-risk hedge, and earnings research scanners.

- [x] Keep all new strategies disabled by default until paper validated.
  Required validation: focused unit tests, full test suite, lint, compile,
  deterministic backtest, fixture replay backtest, and paper-trading dry run.
  Completed: new strategy families remain disabled by default in
  `config/config.yaml`, and tests assert disabled-by-default behavior for the
  added executable strategy families. Earnings-specific additions are exposed
  as disabled read-only research scanners.

- [x] Convert durable follow-up items into Beads tasks when implementation
  begins.
  This `todo.md` is a research plan; active work should use `bd` where
  practical per repo instructions.
  Completed for this implementation pass by creating, claiming, and closing
  repo-local Beads tasks for active TODO implementation work. No open Beads
  tasks remain after the final Phase 8 guardrail review.
