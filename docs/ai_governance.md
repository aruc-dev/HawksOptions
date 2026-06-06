# AI Governance

HawksOptions uses AI only as a veto or audit layer. AI output must never originate a trade, resize a trade, relax a deterministic risk gate, or override `core.risk_manager.pre_trade_check`.

## Contract

- Deterministic strategy constructors create candidate orders.
- Deterministic risk gates decide whether candidates can proceed.
- The local critic, news gate, and optional LLM can only add warnings or veto with `major` severity.
- Provider failures, malformed output, missing keys, or cost-cap exhaustion return a safe non-veto result and are logged for review.

## Audit Evidence

Each scan persists the pre-AI feature packet, deterministic decision, AI decision, and sanitized external review into `reports/ai_disagreements/`. Daily audit packs include those records alongside candidate scans, trade logs, positions, and reconciliation reports.

## Operating Rules

- Keep `ai.enabled: false` until paper evidence shows the additional veto layer reduces false positives without suppressing profitable, risk-approved trades.
- Treat AI disagreement spikes as a review signal, not an execution signal.
- Never put API keys in config files or reports.
