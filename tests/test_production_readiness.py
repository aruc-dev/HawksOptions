from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core.close_executor import close_order_plans
from core.audit_pack import build_audit_pack
from core.config import BASE_DIR, load_config, load_yaml
from core.limit_price import limit_price_improvement_plan
from core.metrics import write_metrics_textfile
from core.models import OptionContract, OrderLeg, StrategyContext, StrategyOrder
from core.order_executor import load_positions, position_from_order, save_positions
from core.reconciler import reconcile_state
from core.risk_manager import _effective_max_portfolio_risk_pct
from core.runtime_guard import assert_runtime_allowed, write_halt_file
from scheduler import run_backtest as run_backtest_cli
from scheduler import run_risk_watch, run_tuning
from strategies.base_strategy import BaseStrategy


class _SizingStrategy(BaseStrategy):
    name = "vertical_spread"

    def generate_order(self, context):
        return None


def _option(symbol: str, bid: float = 1.0, ask: float = 1.1) -> OptionContract:
    return OptionContract(
        contract_symbol=symbol,
        underlying="SPY",
        option_type="put",
        strike=500.0,
        expiration=date(2026, 6, 19),
        bid=bid,
        ask=ask,
        open_interest=500,
        volume=100,
        delta=-0.25,
        underlying_price=510.0,
    )


class _BrokerPositionsClient:
    def __init__(self, positions):
        self.positions = positions

    def get_positions(self):
        return self.positions


class ProductionReadinessConfigTests(unittest.TestCase):
    def test_phase1_portfolio_controls_are_operational_by_default(self):
        config = load_yaml(BASE_DIR / "config" / "config.yaml")

        self.assertEqual(config["portfolio_allocation"]["family_caps_pct"]["short_premium"], 0.15)
        self.assertEqual(config["portfolio_allocation"]["max_single_underlying_allocation_pct"], 0.07)
        self.assertEqual(config["portfolio_concentration"]["max_sector_allocation_pct"], 0.12)
        self.assertTrue(config["portfolio_beta_limits"]["enabled"])
        self.assertEqual(config["portfolio_beta_limits"]["max_abs_spy_beta_delta_pct"], 0.30)
        self.assertEqual(config["risk_throttle"]["max_drawdown_halt_pct"], 10)
        self.assertTrue(config["gates"]["vix_iv_rank_scaling"]["enabled"])
        self.assertTrue(config["position_sizing"]["iv_rank_scaled"]["enabled"])
        self.assertTrue(config["portfolio_vol_targeting"]["enabled"])
        self.assertTrue(config["strategies"]["tail_risk_hedge"]["enabled"])
        self.assertTrue(config["strategies"]["earnings_iron_condor"]["enabled"])

    def test_underlyings_have_production_risk_metadata(self):
        payload = load_yaml(BASE_DIR / "config" / "underlyings.yaml")

        for underlying in payload["underlyings"]:
            self.assertIn("sector", underlying)
            self.assertIn("correlation_group", underlying)
            self.assertIn("beta_to_spy", underlying)
            self.assertIn("tax_classification", underlying)


class ProductionReadinessWorkflowTests(unittest.TestCase):
    def test_backtest_cli_maps_alpaca_history_to_historical_replay(self):
        config = load_config()
        config["backtest"]["fixture_file"] = "tests/fixtures/backtest_market_data.json"
        patched = run_backtest_cli._apply_backtest_source(
            config,
            source="alpaca-history",
            historical_data_file="reports/historical/export.json",
            fixture_fallback_to_sample=True,
        )

        self.assertEqual(patched["backtest"]["data_source"], "historical_replay")
        self.assertEqual(patched["backtest"]["historical_provider"], "alpaca")
        self.assertEqual(patched["backtest"]["historical_data_file"], "reports/historical/export.json")
        self.assertNotIn("fixture_file", patched["backtest"])
        self.assertTrue(patched["backtest"]["fixture_fallback_to_sample"])

    def test_backtest_cli_fixture_source_uses_fixture_not_default_history_file(self):
        config = load_config()
        patched = run_backtest_cli._apply_backtest_source(
            config,
            source="fixture",
            historical_data_file=None,
            fixture_fallback_to_sample=False,
        )

        self.assertEqual(patched["backtest"]["data_source"], "historical_replay")
        self.assertEqual(patched["backtest"]["fixture_file"], "tests/fixtures/backtest_market_data.json")
        self.assertNotIn("historical_data_file", patched["backtest"])
        self.assertNotIn("historical_provider", patched["backtest"])

    def test_tuning_top_runs_prefers_constraint_passing_runs(self):
        runs = [
            {"label": "bad", "sharpe": 4.0, "profit_factor": 0.5, "expectancy": -1.0, "drawdown_pct": 2.0},
            {"label": "good", "sharpe": 1.0, "profit_factor": 1.4, "expectancy": 5.0, "drawdown_pct": 4.0},
        ]

        top = run_tuning._top_runs(runs, limit=1)

        self.assertEqual(top[0]["label"], "good")
        self.assertTrue(top[0]["passes_constraints"])

    def test_iv_rank_scaled_sizing_never_exceeds_contract_or_risk_caps(self):
        config = load_config()
        config["strategies"]["vertical_spread"]["contracts"] = 10
        strategy = _SizingStrategy(config)
        context = StrategyContext(
            underlying={"symbol": "SPY", "max_contracts": 10, "strategies_allowed": ["vertical_spread"]},
            chain=[],
            config=config,
            account={"equity": 10000.0, "portfolio_value": 10000.0},
            iv_rank=30.0,
            as_of=date(2026, 4, 23),
            underlying_price=500.0,
        )

        qty = strategy.risk_scaled_order_quantity(context, unit_max_loss=100.0)

        self.assertEqual(qty, 1)

    def test_vol_targeting_reduces_portfolio_risk_cap_when_realized_vol_is_high(self):
        config = load_config()
        account = {"market_context": {"realized_vol_20d": 0.36}}

        cap = _effective_max_portfolio_risk_pct(config, account)

        self.assertEqual(cap, 0.10)

    def test_liquidity_aware_limit_improvement_concedes_less_on_tight_spreads(self):
        config = load_config()
        sell_leg = OrderLeg(contract=_option("SPY260619P00500000", bid=1.00, ask=1.02), side="sell_to_open")
        buy_leg = OrderLeg(contract=_option("SPY260619P00495000", bid=0.40, ask=0.42), side="buy_to_open")
        order = StrategyOrder(
            strategy_name="vertical_spread",
            strategy_id="vertical_spread-SPY-20260423",
            underlying="SPY",
            legs=[sell_leg, buy_leg],
            max_loss=440.0,
            max_profit=60.0,
            required_buying_power=440.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
            iv_rank=50.0,
        )

        plan = limit_price_improvement_plan(order, config=config)

        self.assertLess(plan["max_acceptable_net_credit"], plan["initial_net_credit"])
        self.assertGreater(plan["max_acceptable_net_credit"], plan["worst_net_credit"])

    def test_risk_watch_persists_snapshot_and_triggers_planning_risk_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            elevated_path = Path(tmp) / "elevated_positions.json"
            paths = {"elevated_positions": elevated_path, "metrics": Path(tmp) / "metrics.prom"}
            with (
                patch.object(run_risk_watch, "load_runtime", return_value=({"mode": "paper"}, object(), paths)),
                patch.object(run_risk_watch, "current_positions", return_value=["position"]),
                patch.object(run_risk_watch, "refresh_positions", return_value=["position"]),
                patch.object(run_risk_watch, "identify_elevated_positions", return_value=["strategy-1"]),
                patch("scheduler.run_risk_check._run_risk_check_with_runtime", return_value={"actions": []}) as risk_check,
            ):
                result = run_risk_watch.run_risk_watch(dry_run=False)

            self.assertEqual(result["elevated_count"], 1)
            self.assertTrue(result["triggered_extra_risk_check"])
            risk_check.assert_called_once()
            persisted = json.loads(elevated_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["elevated_positions"], ["strategy-1"])
            self.assertTrue(persisted["triggered_extra_risk_check"])

    def test_runtime_guard_blocks_halt_file_and_live_without_daily_ack(self):
        with tempfile.TemporaryDirectory() as tmp:
            halt_file = Path(tmp) / "HALTED"
            write_halt_file(halt_file, reason="operator test")
            self.assertEqual(halt_file.stat().st_mode & 0o777, 0o600)
            with self.assertRaisesRegex(RuntimeError, "hawksoptions_halted"):
                assert_runtime_allowed({"mode": "paper"}, halt_file=halt_file)
            halt_file.unlink()
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(RuntimeError, "live_mode_requires_HAWKSOPTIONS_LIVE_ACK"):
                    assert_runtime_allowed({"mode": "live"}, halt_file=halt_file)

    def test_metrics_textfile_writes_prometheus_style_gauges(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_metrics_textfile(Path(tmp) / "hawks.prom", {"elevated-count": 2})

            text = path.read_text(encoding="utf-8")

        self.assertIn("hawksoptions_elevated_count 2.0", text)
        self.assertIn("# HELP hawksoptions_elevated_count", text)
        self.assertNotIn("hawksoptions_runtime_metric", text)

    def test_auto_close_filter_keeps_non_critical_actions_plan_only(self):
        order = StrategyOrder(
            strategy_name="vertical_spread",
            strategy_id="vertical_spread-SPY-20260423",
            underlying="SPY",
            legs=[OrderLeg(contract=_option("SPY260619P00500000"), side="sell_to_open")],
            max_loss=100.0,
            max_profit=20.0,
            required_buying_power=100.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
            iv_rank=50.0,
        )
        position = position_from_order(order, opened_at=datetime(2026, 4, 23, tzinfo=timezone.utc))

        plans = close_order_plans(
            [position],
            [{"strategy_id": position.strategy_id, "action": "take_profit"}],
            client=object(),
            execute_enabled=True,
            dry_run=False,
            allowed_auto_close_actions=["stop_loss", "close_for_ex_div"],
        )

        self.assertTrue(plans[0]["dry_run"])
        self.assertFalse(plans[0]["auto_close_allowed"])
        self.assertEqual(plans[0]["result"]["status"], "planned")

    def test_empty_auto_close_allowlist_allows_no_actions(self):
        order = StrategyOrder(
            strategy_name="vertical_spread",
            strategy_id="vertical_spread-SPY-20260423",
            underlying="SPY",
            legs=[OrderLeg(contract=_option("SPY260619P00500000"), side="sell_to_open")],
            max_loss=100.0,
            max_profit=20.0,
            required_buying_power=100.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.5,
            roll_threshold_delta=-0.4,
            iv_rank=50.0,
        )
        position = position_from_order(order, opened_at=datetime(2026, 4, 23, tzinfo=timezone.utc))

        plans = close_order_plans(
            [position],
            [{"strategy_id": position.strategy_id, "action": "stop_loss"}],
            client=object(),
            execute_enabled=True,
            dry_run=False,
            allowed_auto_close_actions=[],
        )

        self.assertTrue(plans[0]["dry_run"])
        self.assertFalse(plans[0]["auto_close_allowed"])

    def test_reconciler_ingests_broker_only_option_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            positions_path = Path(tmp) / "positions.json"
            reports_dir = Path(tmp) / "reports"
            halt_file = Path(tmp) / "HALTED"

            report = reconcile_state(
                client=_BrokerPositionsClient([
                    {"symbol": "SPY260619P00500000", "qty": "1", "avg_entry_price": "1.25", "current_price": "1.30"}
                ]),
                positions_path=positions_path,
                reports_dir=reports_dir,
                halt_file=halt_file,
            )

            positions = load_positions(positions_path)

        self.assertEqual(report["missing_local"], ["SPY260619P00500000"])
        self.assertEqual(positions[0].strategy_name, "strategy_unknown")
        self.assertEqual(positions[0].strategy_id, "reconciled-SPY260619P00500000")

    def test_reconciler_halts_on_quantity_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            positions_path = Path(tmp) / "positions.json"
            reports_dir = Path(tmp) / "reports"
            halt_file = Path(tmp) / "HALTED"
            order = StrategyOrder(
                strategy_name="vertical_spread",
                strategy_id="vertical_spread-SPY-20260423",
                underlying="SPY",
                legs=[OrderLeg(contract=_option("SPY260619P00500000"), side="buy_to_open", qty=1)],
                max_loss=100.0,
                max_profit=20.0,
                required_buying_power=100.0,
                profit_take_pct=0.5,
                loss_stop_multiple=1.5,
                roll_threshold_delta=-0.4,
                iv_rank=50.0,
            )
            save_positions(positions_path, [position_from_order(order)])

            report = reconcile_state(
                client=_BrokerPositionsClient([
                    {"symbol": "SPY260619P00500000", "qty": "2", "avg_entry_price": "1.25", "current_price": "1.30"}
                ]),
                positions_path=positions_path,
                reports_dir=reports_dir,
                halt_file=halt_file,
            )

            self.assertEqual(report["mismatched_qty"], ["SPY260619P00500000"])
            self.assertTrue(halt_file.exists())
            self.assertRegex(Path(report["report_path"]).name, r"reconciliation_\d{8}-\d{6}-\d{6}\.json")

    def test_reconciler_halts_on_partial_orphaned_multileg_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            positions_path = Path(tmp) / "positions.json"
            reports_dir = Path(tmp) / "reports"
            halt_file = Path(tmp) / "HALTED"
            order = StrategyOrder(
                strategy_name="vertical_spread",
                strategy_id="vertical_spread-SPY-20260423",
                underlying="SPY",
                legs=[
                    OrderLeg(contract=_option("SPY260619P00500000"), side="sell_to_open", qty=1),
                    OrderLeg(contract=_option("SPY260619P00495000"), side="buy_to_open", qty=1),
                ],
                max_loss=100.0,
                max_profit=20.0,
                required_buying_power=100.0,
                profit_take_pct=0.5,
                loss_stop_multiple=1.5,
                roll_threshold_delta=-0.4,
                iv_rank=50.0,
            )
            save_positions(positions_path, [position_from_order(order)])

            report = reconcile_state(
                client=_BrokerPositionsClient([
                    {"symbol": "SPY260619P00500000", "qty": "-1", "avg_entry_price": "1.25", "current_price": "1.30"}
                ]),
                positions_path=positions_path,
                reports_dir=reports_dir,
                halt_file=halt_file,
            )
            positions = load_positions(positions_path)

            self.assertEqual(report["partial_orphan_local"], ["SPY260619P00495000"])
            self.assertTrue(report["halted"])
            self.assertTrue(halt_file.exists())
            self.assertEqual(positions[0].strategy_id, "vertical_spread-SPY-20260423")

    def test_reconciler_halts_and_preserves_orphaned_local_position(self):
        with tempfile.TemporaryDirectory() as tmp:
            positions_path = Path(tmp) / "positions.json"
            reports_dir = Path(tmp) / "reports"
            halt_file = Path(tmp) / "HALTED"
            order = StrategyOrder(
                strategy_name="vertical_spread",
                strategy_id="vertical_spread-SPY-20260423",
                underlying="SPY",
                legs=[OrderLeg(contract=_option("SPY260619P00500000"), side="buy_to_open", qty=1)],
                max_loss=100.0,
                max_profit=20.0,
                required_buying_power=100.0,
                profit_take_pct=0.5,
                loss_stop_multiple=1.5,
                roll_threshold_delta=-0.4,
                iv_rank=50.0,
            )
            save_positions(positions_path, [position_from_order(order)])

            report = reconcile_state(
                client=_BrokerPositionsClient([]),
                positions_path=positions_path,
                reports_dir=reports_dir,
                halt_file=halt_file,
            )
            positions = load_positions(positions_path)

            self.assertEqual(report["orphan_local"], ["SPY260619P00500000"])
            self.assertTrue(report["halted"])
            self.assertTrue(halt_file.exists())
            self.assertEqual(positions[0].strategy_id, "vertical_spread-SPY-20260423")

    def test_reconciler_halts_on_broker_only_short_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            positions_path = Path(tmp) / "positions.json"
            reports_dir = Path(tmp) / "reports"
            halt_file = Path(tmp) / "HALTED"

            report = reconcile_state(
                client=_BrokerPositionsClient([
                    {"symbol": "SPY260619C00500000", "qty": "-1", "avg_entry_price": "1.25", "current_price": "1.30"}
                ]),
                positions_path=positions_path,
                reports_dir=reports_dir,
                halt_file=halt_file,
            )

            self.assertEqual(report["broker_only_short"], ["SPY260619C00500000"])
            self.assertTrue(report["halted"])
            self.assertTrue(halt_file.exists())
            self.assertEqual(load_positions(positions_path), [])

    def test_audit_pack_contains_manifest_and_daily_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            reports = root / "reports"
            scans = reports / "candidate_scans"
            scans.mkdir(parents=True)
            scan = scans / "scan_2026-04-23_120000Z.json"
            scan.write_text('{"accepted":[]}', encoding="utf-8")
            trade_log = root / "data" / "trades.csv"
            trade_log.parent.mkdir()
            trade_log.write_text("timestamp,strategy_id\n", encoding="utf-8")

            pack = build_audit_pack(
                trading_day=date(2026, 4, 23),
                reports_dir=reports,
                data_files=[trade_log],
                output_dir=root / "audit",
            )

            with zipfile.ZipFile(pack) as archive:
                names = set(archive.namelist())

        self.assertIn("manifest.sha256.json", names)
        self.assertIn("reports/candidate_scans/scan_2026-04-23_120000Z.json", names)
        self.assertIn("data/trades.csv", names)


if __name__ == "__main__":
    unittest.main()
