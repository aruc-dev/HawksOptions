from __future__ import annotations

import unittest
from datetime import date

from scheduler.run_scan import scan_market


class RunScanTests(unittest.TestCase):
    def test_dry_run_finds_candidates(self):
        result = scan_market(config={}, as_of=date(2026, 4, 23), dry_run=True)
        self.assertGreaterEqual(result["accepted_count"], 1)


if __name__ == "__main__":
    unittest.main()
