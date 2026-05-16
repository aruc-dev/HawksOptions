# HawksOptions - Agent Implementation Plan (Production Hardening)

## 🎯 Context & Directives for the Agent
You are tasked with hardening the `HawksOptions` automated trading system. This system is options-native, strictly enforces defined-risk trades, and executes via the Alpaca API.

**Strict Agent Directives:**
1. **Safety First:** Do not alter the core safety limits (max 5% single-position risk, max 20% portfolio risk, no 0-1 DTE).
2. **Testing:** Every phase must include `unittest` coverage in the `tests/` directory. Run `python3 -m unittest discover` after completing each phase.
3. **Beads Workflow:** If `bd` (Beads) is initialized, run `bd ready --json`, create tasks for each phase, and update state using `bd update <id> --claim` before modifying files.

---

## 🛠 Phase 1: Portfolio Beta-Weighting & VIX-Aware IV Gating
*Objective: Prevent correlated directional risk and ensure IV-Rank calculations are contextualized by broader market volatility.*

- [x] **1.1 VIX-Aware IV-Rank Scaling**
  - **Target:** `core/risk_engine.py` and `scheduler/run_scan.py`
  - **Action:** Modify the IV-Rank gating logic. Fetch the current VIX level via the Alpaca market data API.
  - **Logic:** Implement a scaling multiplier. If VIX < 15, the minimum IV-Rank threshold for short premium (CSP/Spreads) must be strictly > 50. If VIX > 25, lower the IV-Rank threshold to > 35 to capture structurally inflated premium.
  - **Test:** Mock VIX API responses in `tests/test_risk_engine.py` to ensure the threshold dynamically shifts.

- [x] **1.2 SPY Beta-Weighting Engine**
  - **Target:** `core/portfolio_metrics.py` (Create if missing) and `core/risk_engine.py`
  - **Action:** Build a function `calculate_portfolio_beta(open_positions)` that converts all open option deltas into SPY-beta-weighted deltas.
  - **Logic:** Reject new trade entries if the total portfolio SPY-beta-weighted delta exceeds a configurable threshold. The default example is 50% of total account net liquidation value when the optional gate is enabled.

---

## 🛠 Phase 2: Strategy-Specific Trade Execution Guards
*Objective: Enforce strict mathematical edge on Iron Condors and prevent expiration pin-risk on Vertical Spreads.*

- [x] **2.1 Iron Condor Edge Validation (Premium-to-Width Ratio)**
  - **Target:** `strategies/iron_condor.py`
  - **Action:** Intercept the order construction logic before it is passed to the execution module.
  - **Logic:** Calculate `net_credit / spread_width`. Reject the Iron Condor completely if this ratio is `< 0.30` (30%). Log a warning: `Rejected: IC premium ratio below 30% threshold.`
  - **Test:** Add `test_ic_premium_ratio_rejection` in `tests/test_strategies.py`.

- [x] **2.2 Vertical Spread Early Exit (21 DTE / 50% Profit)**
  - **Target:** `strategies/vertical_spread.py` and `scheduler/roll_checks.py` (or exit manager)
  - **Action:** Implement continuous state monitoring for open vertical spreads.
  - **Logic:** Issue a market-close order immediately if either of these conditions are met:
    1. Current unrealized profit `>= 50%` of the max theoretical profit (initial credit received).
    2. The position reaches `21 DTE` (Days to Expiration).
  - **Context:** This eliminates tail-risk gamma expansion and assignment pin-risk.

---

## 🛠 Phase 3: Execution Slippage & NBBO Fill Auditing
*Objective: Quantify the hidden cost of crossing the bid-ask spread on multi-leg options structures.*

- [x] **3.1 NBBO Snapshotting**
  - **Target:** `core/execution.py` (or equivalent Alpaca order routing module)
  - **Action:** Immediately prior to submitting a live order, fetch the exact National Best Bid and Offer (NBBO) for all legs of the trade.
  - **Logic:** Calculate and log the "Expected Midpoint Price" into the local database/trade logger.

- [x] **3.2 Trade-Log Slippage Reconciliation**
  - **Target:** `core/trade_logging.py`
  - **Action:** During the automated trade-log reconciliation (when the broker confirms the closed/filled order), compare the actual fill price against the stored Expected Midpoint Price.
  - **Logic:** Compute `Slippage = Actual Fill - Expected Midpoint`. Append this data point to the trade's metadata.
  - **Dashboard Integration:** Expose aggregate slippage-per-strategy via the FastAPI dashboard endpoints in `dashboard/app.py`.

---

## 🏁 Final Validation Check
- [x] Run complete test suite: `python3 -m unittest discover -v`
- [x] Run linter: `ruff check .`
- [x] Dry run the risk check: `python3 scheduler/run_risk_check.py --dry-run`
- [x] Dry run the scanner: `python3 scheduler/run_scan.py --dry-run`
- [x] Document all added environment variables or config keys in `config/config.yaml` and `config/.env.example`.
