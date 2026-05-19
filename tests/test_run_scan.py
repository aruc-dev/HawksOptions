from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from core.config import load_config
from core.models import OptionContract, StrategyContext
from scheduler.run_scan import _market_context_for_scan, _symbol_scan_health, scan_market


def _sample_scan_config() -> dict:
    config = load_config()
    config["market_data"]["use_sample_data"] = True
    # Most scan tests validate ranking/report plumbing, not production
    # profitability gates. Keep the fixture permissive so it produces
    # deterministic candidates.
    config["strategies"]["vertical_spread"]["min_credit_to_roundtrip_cost"] = 0
    return config


class _VolatilityClient:
    def __init__(self):
        self.calls = 0

    def get_market_volatility_snapshot(self, *, as_of):
        self.calls += 1
        raise RuntimeError("provider offline")


class _ContextFailureClient:
    def get_account(self):
        return {
            "equity": 100000.0,
            "portfolio_value": 100000.0,
            "cash": 50000.0,
            "buying_power": 100000.0,
            "options_level": 3,
        }

    def get_underlying_snapshot(self, symbol, as_of=None):
        raise TimeoutError("stock quote timeout")


class RunScanTests(unittest.TestCase):
    def test_market_context_lookup_skipped_when_vix_scaling_disabled(self):
        client = _VolatilityClient()
        config = load_config()
        config["gates"]["vix_iv_rank_scaling"]["enabled"] = False

        context = _market_context_for_scan(config=config, client=client, as_of=date(2026, 4, 23))

        self.assertEqual(context, {})
        self.assertEqual(client.calls, 0)

    def test_market_context_lookup_failure_returns_unavailable_snapshot_when_enabled(self):
        client = _VolatilityClient()
        config = load_config()
        config["gates"]["vix_iv_rank_scaling"]["enabled"] = True

        context = _market_context_for_scan(config=config, client=client, as_of=date(2026, 4, 23))

        self.assertEqual(context["vix"], None)
        self.assertEqual(context["source"], "market_volatility_snapshot_error")
        self.assertEqual(context["as_of"], "2026-04-23")
        self.assertEqual(client.calls, 1)

    def test_dry_run_finds_candidates(self):
        config = _sample_scan_config()
        config["strategies"]["iron_condor"]["min_credit_to_roundtrip_cost"] = 0
        result = scan_market(config=config, as_of=date(2026, 4, 23), dry_run=True)
        self.assertGreaterEqual(result["accepted_count"], 1)
        self.assertIn("research_candidates", result)
        self.assertIn("research_traces", result)

    def test_dry_run_ranks_all_risk_approved_candidates(self):
        config = _sample_scan_config()
        config["account"]["max_single_position_risk_pct"] = 1.0
        config["account"]["max_portfolio_risk_pct"] = 1.0
        config["account"]["max_open_strategies"] = 1
        result = scan_market(config=config, as_of=date(2026, 4, 23), dry_run=True)

        self.assertEqual(result["candidate_count"], len(result["ranked_candidates"]))
        self.assertGreater(result["candidate_count"], result["accepted_count"])
        scores = [candidate["score"] for candidate in result["ranked_candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn("score", result["accepted"][0])
        self.assertIn("selection", result["accepted"][0])
        self.assertIn("pre_ai_feature_packet", result["accepted"][0])
        self.assertEqual(result["accepted"][0]["pre_ai_feature_packet"]["schema_version"], 1)

    def test_scan_persists_candidate_set_report(self):
        config = _sample_scan_config()
        config["account"]["max_single_position_risk_pct"] = 1.0
        config["account"]["max_portfolio_risk_pct"] = 1.0
        with TemporaryDirectory() as tmp:
            config["reporting"]["reports_dir"] = tmp
            result = scan_market(config=config, as_of=date(2026, 4, 23), dry_run=True)
            report_path = Path(result["candidate_report_path"])
            rejection_report_path = Path(result["rejection_report_path"])
            scan_health_report_path = Path(result["scan_health_report_path"])
            research_trace_path = Path(result["research_trace_path"])
            ai_disagreement_path = Path(result["ai_disagreement_path"])

            self.assertTrue(report_path.exists())
            self.assertTrue(rejection_report_path.exists())
            self.assertTrue(scan_health_report_path.exists())
            self.assertTrue(research_trace_path.exists())
            self.assertTrue(ai_disagreement_path.exists())
            self.assertEqual(report_path.parent, Path(tmp) / "candidate_scans")
            self.assertEqual(rejection_report_path.parent, Path(tmp) / "rejections")
            self.assertEqual(scan_health_report_path.parent, Path(tmp) / "scan_health")
            self.assertEqual(research_trace_path.parent, Path(tmp) / "research_traces")
            self.assertEqual(ai_disagreement_path.parent, Path(tmp) / "ai_disagreements")
            with report_path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
            rejection_report = rejection_report_path.read_text(encoding="utf-8")
            scan_health_report = scan_health_report_path.read_text(encoding="utf-8")
            with research_trace_path.open("r", encoding="utf-8") as handle:
                research_trace = json.load(handle)
            with ai_disagreement_path.open("r", encoding="utf-8") as handle:
                ai_disagreements = json.load(handle)

        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["candidate_count"], len(payload["ranked_candidates"]))
        self.assertEqual(payload["accepted_count"], result["accepted_count"])
        self.assertEqual(payload["rejected_count"], result["rejected_count"])
        self.assertEqual(payload["generated_candidates"], payload["ranked_candidates"])
        self.assertEqual(payload["chosen_order"], payload["accepted"][0])
        self.assertIn("config_hash", payload)
        self.assertIn("market_snapshot", payload)
        self.assertIn("accepted", payload)
        self.assertIn("rejected", payload)
        self.assertIn("scan_health", payload)
        self.assertIn("research_traces", payload)
        self.assertIn("ai_disagreements", payload)
        self.assertIn("## By Reason", rejection_report)
        self.assertIn("# HawksOptions Scan Health", scan_health_report)
        self.assertEqual(research_trace["schema_version"], 1)
        self.assertEqual(research_trace["trace_count"], len(result["research_traces"]))
        self.assertEqual(research_trace["candidate_count"], len(result["research_candidates"]))
        self.assertEqual(ai_disagreements["schema_version"], 1)
        self.assertEqual(ai_disagreements["summary"]["total"], len(result["ai_disagreements"]))

    def test_scan_logs_deterministic_rejects_before_ai(self):
        config = _sample_scan_config()
        config["account"]["max_single_position_risk_pct"] = 0.000001
        config["account"]["max_portfolio_risk_pct"] = 0.000001
        with TemporaryDirectory() as tmp:
            config["reporting"]["reports_dir"] = tmp
            result = scan_market(config=config, as_of=date(2026, 4, 23), dry_run=True)
            with Path(result["ai_disagreement_path"]).open("r", encoding="utf-8") as handle:
                payload = json.load(handle)

        self.assertGreaterEqual(len(result["ai_disagreements"]), 1)
        first = result["ai_disagreements"][0]
        self.assertEqual(first["type"], "deterministic_reject_before_ai")
        self.assertEqual(first["deterministic_decision"], "reject")
        self.assertEqual(first["ai_decision"], "not_run")
        self.assertIn("feature_packet", first)
        self.assertEqual(payload["summary"]["by_type"]["deterministic_reject_before_ai"], len(result["ai_disagreements"]))

    def test_research_traces_are_read_only_observations(self):
        config = _sample_scan_config()
        config["account"]["max_single_position_risk_pct"] = 1.0
        config["account"]["max_portfolio_risk_pct"] = 1.0
        with TemporaryDirectory() as tmp:
            config["reporting"]["reports_dir"] = tmp
            result = scan_market(config=config, as_of=date(2026, 4, 23), dry_run=True)

        traces = result["research_traces"]
        self.assertGreaterEqual(len(traces), len(result["scan_health"]["symbols_scanned"]))
        self.assertTrue(all("scanner" in item for item in traces))
        self.assertTrue(all("enabled" in item for item in traces))
        self.assertTrue(all("candidate_count" in item for item in traces))
        self.assertEqual(result["accepted_count"], len(result["accepted"]))

    def test_scan_health_summarizes_symbol_data_quality_and_funnel(self):
        config = _sample_scan_config()
        config["account"]["max_single_position_risk_pct"] = 1.0
        config["account"]["max_portfolio_risk_pct"] = 1.0
        with TemporaryDirectory() as tmp:
            config["reporting"]["reports_dir"] = tmp
            result = scan_market(config=config, as_of=date(2026, 4, 23), dry_run=True)

        health = result["scan_health"]
        self.assertEqual(health["candidate_count"], result["candidate_count"])
        self.assertEqual(health["accepted_count"], result["accepted_count"])
        self.assertEqual(health["rejected_count"], result["rejected_count"])
        self.assertGreaterEqual(health["symbol_count"], 1)
        self.assertIn("top_rejection_reasons", health)
        self.assertEqual(len(health["by_symbol"]), health["symbol_count"])
        first_symbol = health["by_symbol"][0]
        self.assertIn("chain_available", first_symbol)
        self.assertIn("contract_count", first_symbol)
        self.assertIn("missing_greeks_contract_count", first_symbol)
        self.assertIn("missing_quote_timestamps", first_symbol)
        self.assertIn("stale_quote_count", first_symbol)
        self.assertIn("candidate_count", first_symbol)
        self.assertIn("accepted_count", first_symbol)
        self.assertIn("rejected_count", first_symbol)

    def test_symbol_scan_health_counts_missing_greeks_and_stale_quotes(self):
        contract = OptionContract(
            contract_symbol="XYZ260619C00100000",
            underlying="XYZ",
            option_type="call",
            strike=100.0,
            expiration=date(2026, 6, 19),
            bid=1.0,
            ask=1.2,
            open_interest=100,
            volume=10,
            implied_volatility=0.25,
            delta=None,
            theta=-0.01,
            vega=None,
            gamma=0.02,
            underlying_price=100.0,
            meta={"quote_timestamp": "2026-04-23T09:58:00+00:00"},
        )
        fallback_contract = OptionContract(
            contract_symbol="XYZ260619P00095000",
            underlying="XYZ",
            option_type="put",
            strike=95.0,
            expiration=date(2026, 6, 19),
            bid=0.0,
            ask=0.0,
            open_interest=100,
            volume=10,
            implied_volatility=0.25,
            delta=-0.2,
            theta=-0.01,
            vega=0.1,
            gamma=0.02,
            underlying_price=100.0,
            meta={"lifecycle_state": "stale_quote_fallback"},
        )
        context = StrategyContext(
            underlying={"symbol": "XYZ"},
            chain=[contract, fallback_contract],
            config={},
            account={},
            iv_rank=50.0,
            as_of=date(2026, 4, 23),
            underlying_price=100.0,
        )

        health = _symbol_scan_health(
            context,
            gates={"max_quote_age_seconds": 60, "max_bid_ask_spread_pct": 0.1},
            as_of=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
        )

        self.assertTrue(health["chain_available"])
        self.assertEqual(health["contract_count"], 2)
        self.assertEqual(health["missing_greeks_contract_count"], 1)
        self.assertEqual(health["missing_greeks_by_field"]["delta"], 1)
        self.assertEqual(health["missing_greeks_by_field"]["vega"], 1)
        self.assertEqual(health["missing_quote_timestamps"], 1)
        self.assertEqual(health["stale_quote_count"], 1)
        self.assertEqual(health["stale_quote_fallback_count"], 1)
        self.assertEqual(health["invalid_quote_count"], 1)
        self.assertEqual(health["wide_quote_count"], 1)

    def test_scan_records_context_failure_without_aborting_symbol_loop(self):
        config = load_config()
        config["gates"]["vix_iv_rank_scaling"]["enabled"] = False
        with TemporaryDirectory() as tmp:
            config["reporting"]["reports_dir"] = tmp
            paths = {
                "positions": Path(tmp) / "positions.json",
                "trade_log": Path(tmp) / "trades.csv",
                "reports_dir": Path(tmp),
            }
            with (
                patch("scheduler.run_scan.load_runtime", return_value=(config, _ContextFailureClient(), paths)),
                patch("scheduler.run_scan.configured_underlyings", return_value=[{"symbol": "BAD"}]),
                patch("scheduler.run_scan.current_positions", return_value=[]),
                patch("scheduler.run_scan.refresh_positions", return_value=[]),
                patch("scheduler.run_scan.build_enabled_strategies", return_value=[]),
            ):
                result = scan_market(config=config, as_of=date(2026, 4, 23), dry_run=True)

        self.assertEqual(result["candidate_count"], 0)
        self.assertEqual(result["rejected"][0]["stage"], "context")
        self.assertEqual(result["rejected"][0]["underlying"], "BAD")
        self.assertIn("context_unavailable:TimeoutError", result["rejected"][0]["reasons"])
        self.assertEqual(result["scan_health"]["chain_unavailable_symbols"], ["BAD"])
        self.assertEqual(result["scan_health"]["by_symbol"][0]["context_error"], "stock quote timeout")


if __name__ == "__main__":
    unittest.main()
