"""Iron-condor strategy."""

from __future__ import annotations

import logging

from core.contract_selector import select_iron_condor
from core.models import OrderLeg, StrategyContext, StrategyOrder
from strategies.base_strategy import BaseStrategy

_LOGGER = logging.getLogger(__name__)


class IronCondorStrategy(BaseStrategy):
    name = "iron_condor"

    def generate_order(self, context: StrategyContext) -> StrategyOrder | None:
        if not self.enabled() or not self.allowed_for_underlying(context):
            return None
        if self.in_earnings_blackout(context):
            return None
        if not self.event_risk_filter_passes(context):
            return None
        min_iv_rank = float(self.params.get("min_iv_rank", 0.0))
        if min_iv_rank > 0 and context.iv_rank < min_iv_rank:
            return None
        if not self.technical_regime_filter_passes(context):
            return None
        if not self.implied_realized_filter_passes(context):
            return None
        if not self.volatility_surface_filter_passes(context, option_type="put"):
            return None
        if not self.volatility_surface_filter_passes(context, option_type="call"):
            return None
        if context.atr_pct > float(self.params.get("max_atr_pct", 0.03)):
            return None
        contracts = select_iron_condor(
            context.chain,
            put_short_delta=float(self.params.get("put_short_delta", -0.18)),
            put_long_delta=float(self.params.get("put_long_delta", -0.08)),
            call_short_delta=float(self.params.get("call_short_delta", 0.18)),
            call_long_delta=float(self.params.get("call_long_delta", 0.08)),
            target_dte=int(self.params.get("target_dte", 40)),
            as_of=context.as_of,
        )
        if contracts is None:
            return None
        short_put, long_put, short_call, long_call = contracts
        credit = round(
            (short_put.mid_price() + short_call.mid_price() - long_put.mid_price() - long_call.mid_price()) * 100.0,
            2,
        )
        put_width = abs(short_put.strike - long_put.strike) * 100.0
        call_width = abs(long_call.strike - short_call.strike) * 100.0
        max_loss = round(max(put_width, call_width) - credit, 2)
        if credit <= 0 or max_loss <= 0:
            return None
        width = max(put_width, call_width)
        min_credit_to_width = float(self.params.get("min_credit_to_width", 0.0))
        if width > 0 and min_credit_to_width > 0 and credit / width < min_credit_to_width:
            _LOGGER.warning("Rejected: IC premium ratio below %.2f threshold.", min_credit_to_width)
            return None
        if not self.credit_quality_passes(credit=credit, width=width):
            return None
        legs = [
            OrderLeg(contract=short_put, side="sell_to_open", qty=1),
            OrderLeg(contract=long_put, side="buy_to_open", qty=1),
            OrderLeg(contract=short_call, side="sell_to_open", qty=1),
            OrderLeg(contract=long_call, side="buy_to_open", qty=1),
        ]
        if not self.cost_adjusted_credit_passes(credit=credit, legs=legs):
            return None
        qty = self.risk_scaled_order_quantity(context, unit_max_loss=max_loss)
        if qty <= 0:
            return None
        order = StrategyOrder(
            strategy_name=self.name,
            strategy_id=self.strategy_id(context),
            underlying=context.underlying["symbol"],
            legs=legs,
            max_loss=max_loss,
            max_profit=credit,
            required_buying_power=max_loss,
            profit_take_pct=float(self.params.get("profit_take_pct", 0.40)),
            loss_stop_multiple=float(self.params.get("loss_stop_multiple", 2.0)),
            roll_threshold_delta=float(self.params.get("warning_delta_abs", 0.40)),
            iv_rank=context.iv_rank,
            required_options_level=int(self.params.get("required_options_level", 3)),
            swing_only=bool(self.params.get("swing_only", True)),
            next_earnings_date=self.next_earnings_date(context),
            ex_dividend_date=self.ex_dividend_date(context),
        )
        return self.apply_contract_quantity(order, qty)
