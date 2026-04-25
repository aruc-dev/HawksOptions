from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dashboard import data_sources


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

    def test_run_check_systemd_missing_script(self):
        with patch.object(data_sources, "cfg") as mock_cfg:
            mock_cfg.return_value.check_systemd_script = Path("/nope/script.sh")
            out = data_sources.run_check_systemd()
        self.assertFalse(out["ok"])


if __name__ == "__main__":
    unittest.main()
