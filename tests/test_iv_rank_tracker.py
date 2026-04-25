from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.iv_rank_tracker import append_iv_snapshot, compute_iv_rank, load_iv_history


class IVRankTrackerTests(unittest.TestCase):
    def test_compute_iv_rank(self):
        self.assertEqual(compute_iv_rank(0.30, [0.20, 0.40]), 50.0)

    def test_append_and_load_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "iv.csv"
            append_iv_snapshot(path, "SPY", 0.20, datetime.now(timezone.utc))
            append_iv_snapshot(path, "SPY", 0.25, datetime.now(timezone.utc))
            values = load_iv_history(path, "SPY")
        self.assertEqual(values, [0.2, 0.25])


if __name__ == "__main__":
    unittest.main()
