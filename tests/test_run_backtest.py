from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path

from core.backtest_engine import (
    _apply_entry_slippage,
    _attribution_report,
    _fill_succeeds,
    _leg_slippage_cost,
    _mark_to_market,
    _richer_metrics,
    _slippage_settings,
    run_backtest,
)
from core.config import load_config
from core.models import OptionContract, OrderLeg, PositionSnapshot, StrategyOrder
from strategies import build_enabled_strategies


def _contract(symbol: str, bid: float, ask: float) -> OptionContract:
    return OptionContract(
        contract_symbol=symbol,
        underlying="SPY",
        option_type="put",
        strike=500.0,
        expiration=date(2026, 6, 19),
        bid=bid,
        ask=ask,
        open_interest=500,
        volume=50,
        underlying_price=510.0,
    )


class _StaticChainClient:
    def __init__(self, chain: list[OptionContract], price: float = 510.0):
        self._chain = chain
        self._price = price

    def get_option_chain(self, underlying: str, as_of=None) -> list[OptionContract]:
        return self._chain

    def get_underlying_snapshot(self, underlying: str, as_of=None) -> dict:
        return {"symbol": underlying, "price": self._price}


class RunBacktestTests(unittest.TestCase):
    def test_backtest_executes_trades(self):
        config = load_config()
        config["strategies"]["iron_condor"]["min_credit_to_roundtrip_cost"] = 0
        result, report_path = run_backtest(config=config, strategies=build_enabled_strategies(config), days=10, starting_fund=10000.0)
        self.assertGreater(result.trade_count, 0)
        self.assertGreater(result.closed_trade_count, 0)
        self.assertTrue(report_path.exists())
        self.assertIn("config_hash", result.provenance)
        self.assertIn("sortino", result.metrics)
        self.assertIn("profit_factor", result.metrics)
        self.assertIn("pnl_by_strategy", result.metrics)
        self.assertIn("return_by_strategy_pct", result.metrics)
        self.assertIn("trade_count_by_strategy", result.metrics)
        self.assertIn("by_strategy", result.attribution)
        self.assertIn("by_symbol", result.attribution)
        self.assertIn("attribution_report_path", result.provenance)
        self.assertTrue(Path(result.provenance["attribution_report_path"]).exists())
        self.assertIn("## Data Provenance", report_path.read_text(encoding="utf-8"))
        self.assertIn("## Metrics", report_path.read_text(encoding="utf-8"))
        self.assertIn("strategy_attribution_10d.md", report_path.read_text(encoding="utf-8"))

    def test_attribution_report_groups_strategy_and_symbol_metrics(self):
        attribution = _attribution_report(
            closed_trades=[
                {
                    "strategy": "iron_condor",
                    "underlying": "SPY",
                    "pnl": 40.0,
                    "hold_days": 2,
                    "entry_slippage": 4.0,
                    "max_loss": 500.0,
                },
                {
                    "strategy": "iron_condor",
                    "underlying": "QQQ",
                    "pnl": -10.0,
                    "hold_days": 4,
                    "entry_slippage": 6.0,
                    "max_loss": 300.0,
                },
            ],
            starting_fund=10000.0,
        )

        strategy = attribution["by_strategy"]["iron_condor"]

        self.assertEqual(strategy["trade_count"], 2)
        self.assertEqual(strategy["total_pnl"], 30.0)
        self.assertEqual(strategy["win_rate"], 50.0)
        self.assertEqual(strategy["average_hold_days"], 3.0)
        self.assertEqual(strategy["total_entry_slippage"], 10.0)
        self.assertEqual(strategy["average_risk_used"], 400.0)
        self.assertEqual(strategy["realized_drawdown"], 10.0)
        self.assertEqual(attribution["by_symbol"]["SPY"]["total_pnl"], 40.0)

    def test_sortino_uses_matching_annualized_return_and_downside_scales(self):
        metrics = _richer_metrics(
            equity_curve=[10000.0, 10100.0, 9999.0, 9799.02],
            closed_trades=[],
            days=3,
            exposure_days=0,
            starting_fund=10000.0,
        )

        self.assertEqual(metrics["sortino"], -21.166)

    def test_slippage_is_charged_on_entry_and_exit_prices(self):
        sell_leg = OrderLeg(contract=_contract("SPY260619P00500000", 1.00, 1.20), side="sell_to_open")
        buy_leg = OrderLeg(contract=_contract("SPY260619P00495000", 0.40, 0.50), side="buy_to_open")
        slippage = {"per_leg_cents": 0.02, "spread_pct": 0.5, "commission_per_contract": 0.65}
        position = PositionSnapshot(
            strategy_id="vertical_spread-SPY-20260423",
            strategy_name="vertical_spread",
            underlying="SPY",
            legs=[sell_leg, buy_leg],
            opened_at=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
            entry_credit=sum(leg.opening_cashflow() for leg in [sell_leg, buy_leg]),
            max_loss=100.0,
            profit_take_pct=0.5,
            loss_stop_multiple=2.0,
            roll_threshold_delta=-0.4,
        )

        self.assertEqual(_leg_slippage_cost(sell_leg, slippage), 7.65)
        self.assertEqual(_leg_slippage_cost(buy_leg, slippage), 5.15)

        _apply_entry_slippage(position, slippage)
        _mark_to_market(
            position,
            _StaticChainClient([sell_leg.contract, buy_leg.contract]),
            date(2026, 4, 24),
            slippage=slippage,
        )

        self.assertEqual(position.entry_credit, 52.2)
        self.assertEqual(position.current_close_cost, 77.8)
        self.assertEqual(position.current_pnl, -25.6)

    def test_fill_model_controls_spread_cost(self):
        leg = OrderLeg(contract=_contract("SPY260619P00500000", 1.00, 1.20), side="sell_to_open")
        mid = {"per_leg_cents": 0.0, "spread_pct": 0.0, "commission_per_contract": 0.0, "fill_model": "mid"}
        bid_ask = {"per_leg_cents": 0.0, "spread_pct": 0.0, "commission_per_contract": 0.0, "fill_model": "bid_ask"}
        illiquid = {
            "per_leg_cents": 0.0,
            "spread_pct": 0.0,
            "commission_per_contract": 0.0,
            "fill_model": "liquidity_based",
        }
        object.__setattr__(leg.contract, "open_interest", 1)
        object.__setattr__(leg.contract, "volume", 0)

        self.assertEqual(_leg_slippage_cost(leg, mid), 0.0)
        self.assertEqual(_leg_slippage_cost(leg, bid_ask), 10.0)
        self.assertEqual(_leg_slippage_cost(leg, illiquid), 20.0)

    def test_failed_fill_probability_is_deterministic(self):
        config = load_config()
        config["backtest"]["slippage"]["failed_fill_probability"] = 1.0
        settings = _slippage_settings(config)
        order = StrategyOrder(
            "cash_secured_put",
            "unit-order",
            "SPY",
            [],
            100.0,
            20.0,
            100.0,
            0.5,
            2.0,
            None,
            50.0,
        )

        self.assertEqual(settings["failed_fill_probability"], 1.0)
        self.assertFalse(_fill_succeeds(order, settings))
        self.assertTrue(_fill_succeeds(order, {**settings, "failed_fill_probability": 0.0}))

    def test_expired_missing_otm_contract_marks_to_zero(self):
        leg = OrderLeg(
            contract=OptionContract(
                contract_symbol="SPY260423P00095000",
                underlying="SPY",
                option_type="put",
                strike=95.0,
                expiration=date(2026, 4, 23),
                bid=1.0,
                ask=1.1,
                underlying_price=100.0,
            ),
            side="sell_to_open",
        )
        position = PositionSnapshot(
            strategy_id="cash_secured_put-SPY-20260423",
            strategy_name="cash_secured_put",
            underlying="SPY",
            legs=[leg],
            opened_at=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
            entry_credit=105.0,
            max_loss=9395.0,
            profit_take_pct=0.5,
            loss_stop_multiple=2.0,
            roll_threshold_delta=-0.4,
        )

        _mark_to_market(position, _StaticChainClient([], price=100.0), date(2026, 4, 23))

        self.assertEqual(position.current_close_cost, 0.0)
        self.assertEqual(position.current_pnl, 105.0)
        self.assertEqual(position.legs[0].contract.meta["lifecycle_state"], "expired_otm")

    def test_expired_missing_itm_contract_marks_to_intrinsic(self):
        leg = OrderLeg(
            contract=OptionContract(
                contract_symbol="SPY260423P00095000",
                underlying="SPY",
                option_type="put",
                strike=95.0,
                expiration=date(2026, 4, 23),
                bid=1.0,
                ask=1.1,
                underlying_price=100.0,
            ),
            side="sell_to_open",
        )
        position = PositionSnapshot(
            strategy_id="cash_secured_put-SPY-20260423",
            strategy_name="cash_secured_put",
            underlying="SPY",
            legs=[leg],
            opened_at=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
            entry_credit=105.0,
            max_loss=9395.0,
            profit_take_pct=0.5,
            loss_stop_multiple=2.0,
            roll_threshold_delta=-0.4,
        )

        _mark_to_market(position, _StaticChainClient([], price=90.0), date(2026, 4, 23))

        self.assertEqual(position.current_close_cost, 500.0)
        self.assertEqual(position.current_pnl, -395.0)
        self.assertEqual(position.legs[0].contract.meta["lifecycle_state"], "expired_itm")


if __name__ == "__main__":
    unittest.main()
