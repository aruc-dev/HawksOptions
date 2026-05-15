from __future__ import annotations

import unittest
from datetime import date

from core.config import load_config
from scheduler.run_scan import scan_market


class RunScanTests(unittest.TestCase):
    def test_dry_run_finds_candidates(self):
        result = scan_market(config={}, as_of=date(2026, 4, 23), dry_run=True)
        self.assertGreaterEqual(result["accepted_count"], 1)

    def test_dry_run_ranks_all_risk_approved_candidates(self):
        config = load_config()
        config["account"]["max_single_position_risk_pct"] = 1.0
        config["account"]["max_portfolio_risk_pct"] = 1.0
        result = scan_market(config=config, as_of=date(2026, 4, 23), dry_run=True)

        self.assertEqual(result["candidate_count"], len(result["ranked_candidates"]))
        self.assertGreater(result["candidate_count"], result["accepted_count"])
        scores = [candidate["score"] for candidate in result["ranked_candidates"]]
        self.assertEqual(scores, sorted(scores, reverse=True))
        self.assertIn("score", result["accepted"][0])
        self.assertIn("selection", result["accepted"][0])


if __name__ == "__main__":
    unittest.main()
