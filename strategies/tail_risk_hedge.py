"""Long-put tail-risk hedge with explicit trigger and premium budget gates."""

from __future__ import annotations

from core.contract_selector import find_by_delta
from core.models import OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy


class TailRiskHedgeStrategy(BaseStrategy):
    name = "tail_risk_hedge"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        if self.in_earnings_blackout(context):
            return None
        if not self._triggered(context):
            return None
        contract = find_by_delta(
            self.filtered_chain(context, "put"),
            float(self.params.get("target_delta", -0.10)),
            target_dte=int(self.params.get("target_dte", 60)),
            as_of=context.as_of,
        )
        if contract is None:
            return None
        debit = round(contract.mid_price() * 100.0, 2)
        if debit <= 0:
            return None
        max_contracts_by_budget = int(self._premium_budget(context) // debit)
        qty = min(self.order_quantity(context), max_contracts_by_budget)
        if qty <= 0:
            return None
        max_loss = round(debit * qty, 2)
        max_profit = round(max(0.0, (contract.strike * 100.0 * qty) - max_loss), 2)
        if max_profit <= 0:
            return None
        return StrategyOrder(
            strategy_name=self.name,
            strategy_id=self.strategy_id(context),
            underlying=context.underlying["symbol"],
            legs=[OrderLeg(contract=contract, side="buy_to_open", qty=qty)],
            max_loss=max_loss,
            max_profit=max_profit,
            required_buying_power=max_loss,
            profit_take_pct=float(self.params.get("profit_take_pct", 1.0)),
            loss_stop_multiple=float(self.params.get("loss_stop_multiple", 1.0)),
            roll_threshold_delta=None,
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 1)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=self.next_earnings_date(context),
            ex_dividend_date=self.ex_dividend_date(context),
            metadata={"trigger": self._trigger_reason(context), "premium_budget": self._premium_budget(context)},
        )

    def _triggered(self, context: StrategyContext) -> bool:
        return bool(self._trigger_reason(context))

    def _trigger_reason(self, context: StrategyContext) -> str:
        drawdown_pct = self._account_pct(context.account, "drawdown_pct")
        if drawdown_pct >= float(self.params.get("min_drawdown_pct", 0.05)):
            return "drawdown"
        daily_loss_pct = self._account_pct(context.account, "daily_loss_pct")
        if daily_loss_pct >= float(self.params.get("min_daily_loss_pct", 0.03)):
            return "daily_loss"
        if context.atr_pct >= float(self.params.get("min_atr_pct", 0.025)):
            return "atr"
        min_iv_over_realized = float(self.params.get("min_iv_over_realized_vol", 1.25))
        if context.realized_vol_20d > 0 and context.current_iv >= context.realized_vol_20d * min_iv_over_realized:
            return "iv_over_realized"
        if bool(context.underlying.get("event_risk", False)):
            return "event_risk"
        return ""

    def _premium_budget(self, context: StrategyContext) -> float:
        equity = float(context.account.get("equity") or context.account.get("portfolio_value") or 0.0)
        return round(equity * float(self.params.get("premium_budget_pct", 0.01)), 2)

    @staticmethod
    def _account_pct(account: dict, key: str) -> float:
        value = float(account.get(key, 0.0) or 0.0)
        return value / 100.0 if value > 1.0 else value
