# HawksOptions — Strategy Maturity & Production Readiness Review

**Reviewer:** Independent audit
**Date:** 2026-05-18
**Scope:** Local working tree at `/Users/arunbabuchandrababu/Desktop/HawksTradeOperations/HawksOptions`
**Method:** Module-by-module read of `core/`, `strategies/`, `scheduler/`, `ai/`, `dashboard/`, `tests/`, `config/`, `scheduler/systemd/`, `scripts/`; executed the validation suite (`unittest`, `ruff`, `compileall`, 30-day backtest).

---

## TL;DR

HawksOptions is an unusually disciplined options-trading codebase. The non-negotiables in `CLAUDE.md` (defined-risk only, max-loss accounting, 7-DTE floor, ex-dividend short-call closure, AI veto-only) are all implemented and enforced in code, not just documentation. The test suite is comprehensive (353 tests passing, ~7.5k lines of test code vs. ~11.5k of production code) and the safety architecture — pre-trade gates, NBBO snapshot requirement for live orders, file-locked persistence, fail-closed AI — is markedly above what most retail options codebases achieve.

The system is **production-ready for paper trading and the read-only dashboard**, but it is **not yet production-ready for live execution at scale**. Three gaps stand out:

1. The deterministic 30-day backtest with the committed paper config returns **-9.36%, profit_factor 0.25, win-rate 6.9%**. Even with the documented "synthetic market data is not a profit forecast" caveat, this is a signal that the rule set as currently parameterized is not edge-positive on the test harness.
2. Several risk capabilities are *built but unconfigured*: `risk_throttle`, `portfolio_concentration`, `portfolio_allocation` family caps, and `portfolio_beta_limits` are all wired through `pre_trade_check` but ship with empty/disabled defaults.
3. The continuous risk loop *plans* close orders, the roll loop *suggests* candidates, and the elevated-risk watch *emits a count* — none act by default (`risk_actions.execute_closes: false`).

Overall grade: **B+** for the codebase as built; **C+** for what is currently *operative* with the committed config.

---

## Subsystem Grades

| Subsystem | Grade | One-line rationale |
|---|---|---|
| Strategy library breadth | A− | 11 defined-risk constructors covering credit/debit, neutral/directional, hedge, earnings. |
| Strategy entry rigor | A | Multi-stage filters: enabled → allowed → blackout → event risk → technical regime → IV/realized → surface skew/term → credit quality → cost-adjusted credit. |
| Risk gating (pre-trade) | A | `pre_trade_check` enforces every CLAUDE.md non-negotiable explicitly; 18 reason codes. |
| Risk action (close/roll) | C+ | Plans and detects; does not execute closes by default; `run_risk_watch` is a no-op alert. |
| Position sizing | B− | Max-loss caps at 5% single / 20% portfolio enforced; no Kelly/volatility-targeted sizing. |
| Execution / NBBO | A− | Fresh-NBBO required for live orders, snapshot persisted, multi-step limit improvement plan. |
| AI veto layer | A | Veto-only invariant honored across deterministic critic + LLM + news gate; fail-closed. |
| Backtest | B− | Deterministic, reproducible, attribution-rich; synthetic data only; no walk-forward. |
| Scheduler / systemd | A− | Five timers with sensible cadences; `flock`-guarded job runner; scan↔risk-check serialized. |
| Secrets & separation | A | Dashboard and trading credentials separated by env-var prefix; `/dev/shm` storage; 600 perms. |
| Dashboard | A− | Read-only by construction, Cloudflare-Access + JWKS verification, allowlist, access log. |
| Tests | A | 353 tests pass, ruff-clean, compileall-clean, deprecation-strict-clean. |
| Observability | B | Excellent per-scan JSON artifacts; EOD report is thin; no metrics pipeline. |
| Documentation | A− | `CLAUDE.md`, `AGENTS.md`, `CODEX.md`, `README.md` crisp and current. |

---

## What's working very well

**Safety architecture, in code, not docs.** `core/risk_manager.py:pre_trade_check` runs liquidity, quote-freshness, DTE-floor, earnings-blackout, IV-rank (with optional VIX scaling), conflict-with-open-position, portfolio-allocation, Greeks, SPY-beta-delta, concentration, and risk-throttle gates — every refusal is a *named* reason code emitted into the rejection report.

**Defined-risk verification at the structural level.** `ai/trade_idea_critic.py` independently verifies (a) credit/debit sign matches premium type, (b) net theta sign matches premium type, (c) short calls aren't below spot and short puts aren't above spot.

**Veto-only AI is taken seriously.** `ai/openai_client.py` implements strict fail-closed paths — any error (network, missing key, daily-cap, malformed JSON) returns `severity == "none"`, so an outage cannot block trading; only an affirmative `major` veto can.

**Live-execution guardrails.** `execute_order` raises `RuntimeError("fresh_nbbo_required_for_live_order")` if a complete NBBO snapshot wasn't captured. `close_order_plans` does the same for live closes. Combined with `risk_actions.execute_closes: false`, this is a deliberate "explicit human approval to execute" posture.

**Concurrency hygiene.** `locked_open` + `atomic_write_text` for `positions.json` and trade log; `scripts/run_hawksoptions_job.sh` uses `flock` to serialize `run_scan` and `run_risk_check`. Trade log is appended *before* `positions.json` is rewritten, so a crash leaves the forward-only log as source of truth.

**Validation harness actually runs.** 353 tests in ~12s, all pass. `ruff check .` clean. `compileall` clean. Deprecation-strict pass clean. The 30-day backtest runs to completion.

**Honest backtest disclaimer.** The README tells the user: *"Rule fidelity is decent. Market fidelity is not."* — right epistemics for a synthetic harness.

---

## Concrete gaps and risks

### 1. Backtest result is materially negative on default config

`python3 scheduler/run_backtest.py --days 30 --fund 10000`:

```
total_return_pct: -9.36
sharpe: -11.24
max_drawdown_pct: 9.41
win_rate: 6.9
profit_factor: 0.25
max_consecutive_losses: 32
expectancy: -16.13
```

Only `vertical_spread` fires (58 closed trades, 6.9% win rate). Synthetic data, but the rule set under deterministic inputs produces a >9% drawdown in 30 days. Without a real-data parallel, there is no evidence the parameter set is edge-positive.

### 2. Capabilities present, policies absent

Wired in `pre_trade_check` but absent or disabled in `config/config.yaml`:

- `portfolio_concentration` — no block.
- `portfolio_allocation` (family caps, per-underlying caps) — no block.
- `portfolio_beta_limits` — `enabled: false`.
- `risk_throttle` (drawdown halts, daily-loss halts, size reduction) — no block.
- `vix_iv_rank_scaling` — `enabled: false`.

Effect: today the only hard portfolio-level constraint is `max_portfolio_risk_pct: 0.20` + `max_single_position_risk_pct: 0.05` + `max_open_strategies: 8`. A correlated cluster of eight 5% tech-spread positions could put 40% of equity on one factor without tripping a gate.

### 3. Continuous loop is observational, not corrective

- `run_risk_watch.py`: returns a count of elevated positions.
- `run_roll_check.py`: emits candidates; never builds a roll plan.
- `run_risk_check.py`: builds close-order plans every 5 min; with `execute_closes: false`, plans stay as `{"status": "planned"}`.

Defensible for paper mode; not enough for live.

### 4. EOD report is thin

`run_eod_report.py` writes four lines: open count, portfolio value, trade-log row count, aggregate Greeks. Per-strategy PnL, win-rate, slippage, drawdown vs. baseline, rejection volume are available but not surfaced.

### 5. CSP and covered_call defined-risk caveat

`max_loss = strike*100 - credit` and `cost_basis*100*qty - credit` are *bounded* losses, not "defined-risk" in the same sense as a vertical/condor. The README correctly disables both in the committed paper profile. Either the rule should say "defined-risk OR cash-/stock-secured" or the registry should exclude them.

### 6. Single-broker assumption

`core/alpaca_options_client.py` is the only `TradingClient` implementation. No failover, no read-only fallback chain.

### 7. Reproducible scan artifacts vs. storage growth

`reports/` collects per-scan candidate scans, rejection summaries, scan-health, research traces, AI-disagreement logs every 30 minutes. No retention policy, no compression, no pruning.

### 8. No metrics pipeline

Excellent JSON artifacts; no Prometheus, no StatsD, no CloudWatch, no alerting integration.

### 9. Secrets on tmpfs only protects disk-resident attackers

`/dev/shm/.hawksoptions.env` with `chmod 600` is right, but it remains readable to anything running as `ec2-user`. IAM-instance-profile + per-process fetch would be tighter for multi-tenant hosts.

### 10. `dashboard.create_app` raises at import-time on misconfig

`assert_production_auth_safe()` is called at module load. A misconfigured Cloudflare auth means uvicorn refuses to start — correct, but the failure is easy to miss without `journalctl -u hawksoptions-dashboard`.

---

# Phased Improvement Plan

**Objective: maximize profit while reducing risk; reach production-grade live trading.** Each phase is sequenced so earlier work creates the evidence needed for later changes. No phase relaxes a CLAUDE.md non-negotiable.

## Phase 1 — Calibrate and constrain (2–3 weeks)

Turn "capability exists" into "operative policy"; prove edge before increasing aggressiveness.

1. **Activate unwired portfolio gates** in `config/config.yaml`:
   - `portfolio_allocation.family_caps_pct: {short_premium: 0.15, long_premium: 0.05, earnings_event: 0.05, hedge: 0.02}`.
   - `portfolio_allocation.max_single_underlying_allocation_pct: 0.07`.
   - `portfolio_concentration.max_sector_allocation_pct: 0.12`; add `sector` to each entry in `underlyings.yaml`.
   - `portfolio_concentration.max_correlation_group_allocation_pct: 0.15`.
   - `risk_throttle.max_drawdown_halt_pct: 10`; `daily_loss_halt_pct: 4`; `reduce_risk_drawdown_pct: 5`; `max_throttled_position_risk_pct: 2.5`.
   - `portfolio_beta_limits.enabled: true`, `max_abs_spy_beta_delta_pct: 0.30`; provide `symbol_betas` per underlying.
2. **Real-data backtest.** Drive `historical_market_data` from Alpaca option historicals; add `scheduler/run_backtest.py --source alpaca-history --start 2024-01-01 --end 2025-12-31`.
3. **Parameter sweep.** Extend `scheduler/run_tuning.py` to grid-search `vertical_spread` deltas/DTE/profit-take/stop-multiple. Optimize Sharpe + profit-factor subject to max-DD ≤ 8% and expectancy > 0. Persist top 5 to `reports/tuning/`.
4. **Action the elevated-risk watch.** `run_risk_watch` writes `data/elevated_positions.json` and triggers an extra (still planning-only) `run_risk_check` cycle while elevated > 0.
5. **Beef up EOD report.** Per-strategy PnL, per-symbol PnL, win-rate, slippage, drawdown vs. baseline, rejection-by-reason, AI-disagreement count.
6. **Retention policy.** `scripts/prune_reports.sh`: gzip > 7d, delete > 90d. Daily systemd timer.

**Acceptance:** real-data backtest produces a config with positive expectancy and profit_factor > 1.2 on the chosen window.

## Phase 2 — Lift the profit ceiling at constant or lower risk (4–6 weeks)

1. **IV-rank–scaled position sizing inside the 5% cap.** Higher IV-rank → larger position, never exceeding the cap.
2. **Volatility-targeted overall exposure.** Adjust `max_portfolio_risk_pct` against SPY realized 20d-vol; the hook (`_market_volatility_context`) already exists for IV gating.
3. **Activate `vix_iv_rank_scaling`.** Use the index feed (or a calibrated VIXY proxy).
4. **Enable `tail_risk_hedge`** at `premium_budget_pct: 0.01`. Reduces left-tail at bounded carry cost.
5. **Earnings-IV-crush condor** on a small whitelist of tickers with reliable crush profile from the Phase 1 backtest; cap to `max_contracts: 1` and `earnings_event` family at 5%.
6. **Roll execution.** Implement `core/roll_engine.build_roll_plan` against fresh chains; wire to `run_roll_check` plan-only.
7. **Liquidity-aware limit-price improvement.** Aggressive on tight spreads; concede faster on wide ones. Uses `core/execution_quality.py` data.
8. **Per-strategy `min_credit_to_roundtrip_cost`** on every credit strategy (already on iron condor; add to vertical at `1.15`).

**Acceptance:** real-data 12-month backtest improves expectancy ≥ 25% with max-DD not worse than Phase 1.

## Phase 3 — Production scale and resilience baseline (6–8 weeks)

1. **Second broker (read-only)** as a fallback quote source (Tradier/Polygon). Order submission stays Alpaca-only.
2. **Metrics + alerting.** `core/metrics.py` → Prometheus textfile or CloudWatch EMF. Scans/min, rejects/min, elevated_count, daily_loss_pct, slippage p50/p95, AI veto rate.
3. **Auto-close on critical conditions only.** Flip `execute_closes: true` for `stop_loss` and `close_for_ex_div` only. Circuit breaker: > 3 auto-closes in 15 min → halt + ack.
4. **Kill-switch UX.** `scripts/kill.sh` writes `/etc/hawksoptions/HALTED`; `load_runtime` checks; dashboard surfaces "system halted".
5. **Live-mode gating.** `mode: live` requires `HAWKSOPTIONS_LIVE_ACK=YYYY-MM-DD` matching today AND zero open P1 Beads tasks.
6. **Walk-forward harness.** Quarterly real-data refit; held-out validation; persist to `config/profiles/<quarter>.yaml`.
7. **Stress / scenario tests.** VIX spike, gap-down, weekend gap, partial fill, broker disconnect, stale NBBO mid-submit.
8. **Log rotation + retention** for `reports/*.json`.

**Acceptance:** 30 consecutive paper trading days without manual intervention except daily check-in; max-DD < target; no critical false alerts.

---

# Is Phases 1–3 enough to go live in production?

**No.** Phases 1–3 take HawksOptions from "well-built paper system" to "live-tradable by a disciplined solo operator at small size." That is a meaningful milestone but is not production-grade live trading.

What Phases 1–3 do **not** cover:

- **State recovery & reconciliation.** If the EC2 host crashes mid-fill, `positions.json` may disagree with the broker. No startup reconciler.
- **Market microstructure.** No handling for halts (LULD, news), hard-to-borrow on the equity leg of CCs, mini-options, exchange holidays vs. early closes (1pm ET), late-day liquidity collapse, OPRA outages, exchange-mandated cancellations.
- **Corporate actions.** Splits, special dividends, mergers, ticker changes, OCC contract adjustments are not detected.
- **Regulatory & tax.** No PDT enforcement at the order level (only the pre-trade gate flag), no wash-sale tracking across closes/rolls, no Section 1256 vs. equity-option classification, no 1099-B reconciliation.
- **Disaster recovery.** Single EC2, single AZ, single broker. No region failover, no documented RTO/RPO.
- **Observability & on-call.** Phase 3 adds metrics; it does not add paging, runbooks, escalation.
- **Change management.** No protected branches, no required reviews on `core/risk_manager.py` / `core/order_executor.py`, no canary deploy, no auto-rollback.
- **Paper-to-live promotion ceremony.** Phase 3 hints at gating; real promotion needs a checklist, sign-off, graduated capital ramp.
- **Operational rituals.** Start-of-day, end-of-day, weekly reconciliation aren't codified.

Phases 4, 5, 6 add those layers; the gate at the end fences them off behind a single, explicit go-live checklist.

## Phase 4 — State integrity, recovery, and broker truth (3–4 weeks)

Goal: at any moment, after any failure, the system can answer "what do I actually own at the broker?" and bring local state into agreement, refusing to trade until it does.

1. **Startup reconciler.** New `core/reconciler.py` runs at the head of every scheduler job. Pulls authoritative open option positions and pending orders from Alpaca, diffs against `data/positions.json`:
   - Broker has positions missing locally → ingest as `PositionSnapshot` with `reconciled=true`; try to identify originating strategy from trade log; mark `strategy_unknown` if not found.
   - Local has positions missing at broker → mark closed at the last known broker confirmation; emit `reconciliation_orphan_local` alert.
   - Disagreement on max_loss / qty / strike / expiration → halt the system (`/etc/hawksoptions/HALTED`), require manual ack.
   - Persist the diff to `reports/reconciliation/<timestamp>.json` every run.
2. **Idempotent order submission.** Derive `client_order_id` from `(strategy_id, attempt_n, sha256(payload))`; persist before submit; reject duplicates broker-side. Prevents double-submit on retry-after-timeout.
3. **Trade-log replay.** `scripts/rebuild_positions.py` scans the trade log forward and reconstructs `positions.json`. Test: rebuild on a fixture and assert identical state.
4. **Corporate action ingestion.** Daily pre-open job that fetches OCC/CBOE/Alpaca corporate-action feed; on split adjusts strike/qty/premium; on special dividend re-evaluates ex-div close immediately; on merger/ticker change halts the underlying; unknown action halts the underlying.
5. **Halt / LULD awareness.** Scanner skips halted underlyings; risk-check halts close attempts when halted; halt status visible in dashboard.
6. **Early-close calendar.** Honor NYSE half-day cutoffs (12:15 PM ET instead of 3:15 PM ET).
7. **OCC symbol parsing hardening.** `core/occ.py` assumes standard OPRA; add tests for adjusted symbols, mini-options, weekly conventions. Refuse-to-trade on any symbol the parser doesn't fully understand.

**Acceptance:** simulated process kills at 10 chosen points (mid-scan, post-submit-pre-log, post-log-pre-positions-write, etc.) → startup-reconciler-clean state with zero spurious / zero missing positions.

## Phase 5 — Regulatory, tax, and audit (3–4 weeks)

1. **PDT enforcement at the order layer.** Day-trade counter that watches actual fills and refuses a 4th day-trade in 5 rolling business days, regardless of strategy flags. Different rule, different code path from `pdt_swing_only_violation`.
2. **Wash-sale accounting.** Persist every close to `data/wash_sale_ledger.csv` keyed by substantially-identical (underlying × type × strike × expiration window). On every new entry, check the 30-day window and tag order metadata `wash_sale_risk=true` (advisory in v1).
3. **Section 1256 classification.** Broad-based-index options get 60/40 treatment; equity options don't. Add `tax_classification` to each underlying's metadata and propagate to trade log.
4. **1099-B reconciliation tool.** `scripts/reconcile_1099b.py` matches the year-end 1099-B CSV to trade-log rows; discrepancies flagged.
5. **Audit trail completeness.** `scripts/build_audit_pack.py` produces a sealed, sha256-hashed ZIP per trading day containing scan → candidate → score → pre-trade → critic → external AI → NBBO → limit plan → fills → close lineage → wash-sale flag → tax classification.
6. **Reg-T margin model for multi-leg.** Predict margin locally; on first submission compare against Alpaca's actual margin response and adjust local model if divergent.
7. **AI governance one-pager** at `docs/ai_governance.md`: veto-only contract, fail-closed defaults, spend cap, disagreement audit log. What an examiner will ask for.

**Acceptance:** a randomly-chosen production day's audit pack reproduces, line by line, the brokerage statement for that day. A tax-prep dry run on a synthetic year produces a 1099-B-shaped output that ties.

## Phase 6 — Resilience, on-call, and change management (4–6 weeks)

1. **Multi-AZ or warm-standby.** Active-passive across two AZs with leader election (DynamoDB lock / etcd) **or** 15-min cold-standby procedure (weekly AMI snapshot; `data/` to S3 every 5 min; runbook). Commit to RTO ≤ 30 min, RPO ≤ 5 min for `data/`.
2. **Backups with restore tests.** Versioned S3 backup of `data/`, `config/`, `reports/`. Quarterly restore drill on clean AMI, reconcile, dry-run scan, compare to live.
3. **Broker outage protocol.** `alpaca_reachable == false` for > 5 min → halt new entries; > 15 min → page operator with at-risk positions; never silent infinite retry.
4. **Quote outage protocol.** `stale_quote_fallback_count > 10%` of an underlying's chain for > 3 consecutive scans → drop from candidate pool until clean for 15 min.
5. **Alerting and on-call.** P1 (auto-page): kill-switch tripped, reconciler diff > 0, daily-loss > 4%, no scan in 60 min during market hours, NBBO failure on submit, AI cost cap hit. P2 (Slack/email): elevated_count ≥ 3, single-position max-DD > 50%, AI veto-rate spike. P3 (digest): rejection-rate anomaly, slippage p95 drift. PagerDuty or Opsgenie integration.
6. **Runbooks per alert.** Every alert links to `docs/runbooks/<alert>.md` with check / type / escalate / roll-back. Untriaged alert = no alert.
7. **GitHub protections.** Required-review ≥ 1, ≥ 2 for `core/risk_manager.py`, `core/order_executor.py`, `core/close_executor.py`, `core/alpaca_options_client.py`, `config/config.yaml`. Required CI green. No force-push to `main`. Signed commits. CODEOWNERS.
8. **Canary deploy.** New AMI runs in paper mode for 24h before live promotion. `scripts/promote_canary.sh` includes rollback path. Health-check fail in first 30 min auto-rolls-back.
9. **Configuration diff at startup.** `load_config` logs structured diff vs. previous-effective; any change to risk caps, `mode`, `execute_closes`, or `ai.enabled` triggers a P1.
10. **Time-of-day guardrails.** No new entries in first 15 min after open or last 30 min before close.
11. **Capacity planning.** Calculate per-scan Alpaca API budget against rate limits; alert at > 70% projected utilization.

**Acceptance:** simulated AZ failure recovers within RTO; simulated quote outage on 3 underlyings degrades gracefully without halting unaffected ones; ≤ 5 P2/P3 noise and 0 P1 false positives over a representative 5-day window.

---

# Production-readiness gate (the paper-to-live ceremony)

Do not flip `mode: live` until *all* of the following are true.

**Code & tests**
- [ ] Phases 1–4 acceptance criteria met.
- [ ] Phase 5 acceptance: audit pack reproduces brokerage statement.
- [ ] Reconciler clean for 14 consecutive paper trading days.
- [ ] Zero P1 in 14 consecutive paper trading days.
- [ ] 100% of risk-critical files require review.

**Strategy**
- [ ] Real-data 12-month backtest: positive expectancy, profit_factor ≥ 1.3, max-DD ≤ 8%, parameters chosen by walk-forward not in-sample.
- [ ] Out-of-sample 3-month period validates within 25% of in-sample expectancy.
- [ ] All stress tests pass.

**Operational**
- [ ] Backups restored successfully in last 30 days.
- [ ] Runbook exists for every P1 alert.
- [ ] On-call rotation staffed with documented escalation.
- [ ] Kill-switch tested end-to-end in last 7 days.
- [ ] DR drill completed in last 90 days.

**Regulatory**
- [ ] PDT counter active; live-tested.
- [ ] Wash-sale ledger populated from ≥ 6 months of paper history.
- [ ] Tax classification populated for every active underlying.
- [ ] AI governance one-pager filed.

**Capital ramp (a calendar, not a config flag)**
- Week 1 live: $1k notional cap, max 1 open strategy, daily operator review.
- Weeks 2–3: $5k, max 2 open, daily review.
- Weeks 4–6: $25k, max 4 open, every-other-day review.
- Weeks 7+: graduate to configured caps **only if** every prior tranche produced expected (±2σ) PnL distribution and zero P1 alerts attributable to the system.
- A failed tranche reverts to the prior level for 2 weeks, not zero.

---

# Ongoing rituals after go-live

- **Start-of-day (T-30 min):** `bd ready --json`; dashboard health green; no overnight P1; no unreconciled positions; no halt-news on holdings; no surprise earnings inside 5 DTE.
- **End-of-day:** EOD report reviewed; daily-loss in band; slippage p95 in band; no actionable AI disagreements.
- **Weekly:** reconcile broker statement vs. trade log; rejection-by-reason trend; elevated-count distribution; AI veto-rate.
- **Monthly:** parameter drift (current expectancy vs. backtest); audit-pack spot-check; secrets rotation; AMI rebuild; backup restore test.
- **Quarterly:** protected-path review (who has merge rights, is it still right?); full DR drill; walk-forward refit.

---

# Specific code-level fixes worth doing before Phase 1

- `scheduler/run_eod_report.py`: replace the 4-line summary with per-strategy / per-symbol attribution from `core/backtest_engine.py`.
- `core/risk_manager.identify_elevated_positions`: extract the shared predicate currently duplicated against `continuous_risk_checks` to avoid drift.
- `scheduler/run_risk_watch.py`: persist `elevated_positions` snapshot to disk so the dashboard doesn't recompute.
- `core/close_executor.build_close_order_payload`: add a small limit-improvement lane mirroring `core/limit_price.limit_price_improvement_plan`. Closes are where slippage hurts most.
- `strategies/cash_secured_put.py` & `covered_call.py`: class-level docstring stating bounded-loss but not defined-risk-spread.

---

# Validation evidence captured during this review

- `python3 -m unittest discover -v`: 353 tests, 2 skipped (fastapi not installed in one dashboard module), 0 failures, ~12s.
- `python3 -W error::DeprecationWarning -m unittest discover`: 353 pass, 2 skip; no deprecation surprises.
- `ruff check .`: All checks passed.
- `python3 -m compileall core strategies scheduler ai tests dashboard scripts`: clean.
- `python3 scheduler/run_backtest.py --days 30 --fund 10000`: completes; metrics summarized above. Reports written to `reports/backtest_30d.md` and `reports/strategy_attribution_30d.md`.

---

# Closing note

The codebase reads like the work of someone who has had to operate options strategies before and built the system they wished they'd had. The discipline shows in small places — the AI critic separates "major" from "minor" with intent, the close executor refuses to submit without a complete NBBO, the secrets fetcher requires paper keys even if live keys are missing. The honest README disclaimer about backtest fidelity is a strong signal the maintainer is not over-claiming.

The single biggest unlock for "maximum profit with reduced risk" is **Phase 1, step 2 and 3**: a real-data backtest and parameter sweep. Without that, every other improvement is a guess.

The single biggest unlock for **"production-grade live trading"** is **Phase 4**: a state reconciler that makes the broker the source of truth. Until that exists, every other safety control is reasoning over a local state that can disagree with reality, and any sufficiently bad outage will produce a state divergence the system cannot detect — which is the failure mode that ends trading systems.
