from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import data_sources
from dashboard.config import DashboardConfig


class DataSourceTests(unittest.TestCase):
    def test_read_positions_snapshot_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.json"
            self.assertEqual(data_sources.read_positions_snapshot(path), [])

    def test_read_daily_baseline_invalid_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "baseline.json"
            path.write_text("[]")
            self.assertIsNone(data_sources.read_daily_baseline(path))

    def test_build_iv_rank_heatmap(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "iv.csv"
            path.write_text(
                "timestamp,symbol,implied_volatility\n"
                "2026-04-20T00:00:00+00:00,SPY,0.20\n"
                "2026-04-21T00:00:00+00:00,SPY,0.30\n"
            )
            heatmap = data_sources.build_iv_rank_heatmap(path)
        self.assertEqual(heatmap[0]["symbol"], "SPY")
        self.assertEqual(heatmap[0]["iv_rank"], 100.0)

    def test_latest_health_snapshot_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = data_sources.read_latest_health_snapshot(Path(tmp))
        self.assertFalse(out["ok"])

    def test_recent_log_issues_do_not_expose_raw_log_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            logs = Path(tmp)
            (logs / "risk.log").write_text("ERROR traceback detail with secret\n", encoding="utf-8")

            out = data_sources.read_recent_log_issues(logs)

        self.assertEqual(out[0]["level"], "ERROR")
        self.assertNotIn("traceback detail", out[0]["line"])

    def test_health_snapshot_dir_uses_configured_reports_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.yaml"
            reports_dir = Path(tmp) / "custom_reports"
            config_path.write_text(f"reporting:\n  reports_dir: {reports_dir}\n", encoding="utf-8")

            cfg = DashboardConfig(config_path)
            self.assertEqual(cfg.health_snapshot_dir, reports_dir / "health_snapshots")

    def test_read_latest_rejection_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            scan_dir = reports / "candidate_scans"
            scan_dir.mkdir()
            (scan_dir / "scan_2026-04-23_100000000000Z.json").write_text(
                '{"rejected": ['
                '{"strategy": "iron_condor", "stage": "risk", "reasons": ["dte_gate_failed"]},'
                '{"strategy": "iron_condor", "stage": "risk", "reasons": ["dte_gate_failed", "liquidity_gate_failed"]}'
                ']}',
                encoding="utf-8",
            )

            out = data_sources.read_latest_rejection_summary(reports)

        self.assertTrue(out["ok"])
        self.assertEqual(out["summary"]["total_rejected"], 2)
        self.assertEqual(out["summary"]["by_reason"]["dte_gate_failed"], 2)
        self.assertEqual(out["summary"]["by_strategy"]["iron_condor"], 2)

    def test_read_latest_rejection_summary_handles_invalid_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            scan_dir = reports / "candidate_scans"
            scan_dir.mkdir()
            (scan_dir / "scan_2026-04-23_100000000000Z.json").write_text("{", encoding="utf-8")

            out = data_sources.read_latest_rejection_summary(reports)

        self.assertFalse(out["ok"])
        self.assertEqual(out["summary"]["total_rejected"], 0)
        self.assertIn("error", out)
        self.assertNotIn("Expecting", out["error"])

    def test_read_dashboard_analytics_uses_latest_reports(self):
        with tempfile.TemporaryDirectory() as tmp:
            reports = Path(tmp)
            scan_dir = reports / "candidate_scans"
            scan_dir.mkdir()
            (scan_dir / "scan_2026-04-23_100000000000Z.json").write_text(
                '{"candidate_count": 2, "accepted_count": 1, "rejected_count": 1,'
                '"research_candidates": [{}], "ranked_candidates": [{"underlying": "SPY"}],'
                '"chosen_orders": [{"underlying": "SPY"}],'
                '"scan_health": {"symbol_count": 1}}',
                encoding="utf-8",
            )
            (reports / "strategy_attribution_30d.md").write_text(
                "# Attribution\n\n```json\n{\"by_strategy\": {\"iron_condor\": {\"total_pnl\": 1}}}\n```\n",
                encoding="utf-8",
            )
            drift_dir = reports / "drift"
            drift_dir.mkdir()
            (drift_dir / "paper_vs_backtest_30d.md").write_text(
                "# Drift\n\n```json\n{\"drift\": {\"total_slippage_delta\": 1}}\n```\n",
                encoding="utf-8",
            )
            research_dir = reports / "research_traces"
            research_dir.mkdir()
            (research_dir / "research_trace_2026-04-23_100000000000Z.json").write_text(
                '{"trace_count": 1, "traces": [{"scanner": "earnings_calendar_spread"}]}',
                encoding="utf-8",
            )
            disagreement_dir = reports / "ai_disagreements"
            disagreement_dir.mkdir()
            (disagreement_dir / "ai_disagreements_2026-04-23_100000000000Z.json").write_text(
                '{"summary": {"total": 1}, "disagreements": [{"type": "deterministic_reject_before_ai"}]}',
                encoding="utf-8",
            )
            with patch.object(data_sources, "cfg") as mock_cfg:
                mock_cfg.return_value.reports_dir = reports
                mock_cfg.return_value.account_config = {
                    "max_portfolio_risk_pct": 0.2,
                    "max_single_position_risk_pct": 0.05,
                }
                out = data_sources.read_dashboard_analytics(
                    [{"strategy_id": "s1", "max_loss": 100.0}],
                    {"portfolio_value": 1000.0},
                )

        self.assertEqual(out["candidate_funnel"]["candidate_count"], 2)
        self.assertEqual(out["candidate_funnel"]["research_candidate_count"], 1)
        self.assertEqual(out["scan_health"]["data"], {"symbol_count": 1})
        self.assertEqual(out["strategy_attribution"]["data"]["by_strategy"]["iron_condor"]["total_pnl"], 1)
        self.assertEqual(out["drift"]["data"]["drift"]["total_slippage_delta"], 1)
        self.assertEqual(out["research_trace"]["data"]["trace_count"], 1)
        self.assertEqual(out["ai_disagreements"]["data"]["summary"]["total"], 1)
        self.assertEqual(out["risk_budget"]["portfolio_cap_remaining"], 100.0)

    def test_run_check_systemd_missing_script(self):
        with patch.object(data_sources, "cfg") as mock_cfg:
            mock_cfg.return_value.check_systemd_script = Path("/nope/script.sh")
            out = data_sources.run_check_systemd()
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
