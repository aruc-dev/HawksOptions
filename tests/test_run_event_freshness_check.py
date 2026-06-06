from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from scheduler import run_event_freshness_check as event_check


class RunEventFreshnessCheckTests(unittest.TestCase):
    def test_check_event_freshness_excludes_sanitized_underlying_payload(self):
        with patch.object(
            event_check,
            "load_underlyings",
            return_value=[{"symbol": "AAPL", "next_earnings_date": "2026-05-02"}],
        ):
            payload = event_check.check_event_freshness(
                config={"underlyings": {"refresh_earnings_dates_days": 7}},
                as_of=date(2026, 6, 6),
            )

        self.assertEqual(payload["stale_event_count"], 1)
        self.assertNotIn("sanitized_underlyings", payload)

    def test_main_fails_when_fail_on_stale_finds_stale_dates(self):
        config = {"underlyings": {"refresh_earnings_dates_days": 7}, "reporting": {}}
        with (
            patch.object(event_check, "load_config", return_value=config),
            patch.object(
                event_check,
                "load_underlyings",
                return_value=[{"symbol": "AAPL", "next_earnings_date": "2026-05-02"}],
            ),
            redirect_stdout(io.StringIO()) as stdout,
        ):
            code = event_check.main(["--as-of", "2026-06-06", "--no-report", "--fail-on-stale"])

        payload = json.loads(stdout.getvalue())
        self.assertEqual(code, 1)
        self.assertEqual(payload["status"], "stale")

    def test_persist_event_freshness_report_writes_json_and_markdown(self):
        with TemporaryDirectory() as tmp:
            config = {
                "reporting": {
                    "reports_dir": tmp,
                    "logs_dir": f"{tmp}/logs",
                    "trade_log_file": f"{tmp}/trades.csv",
                    "positions_file": f"{tmp}/positions.json",
                    "greeks_snapshot_dir": f"{tmp}/greeks",
                }
            }
            payload = {
                "as_of": "2026-06-06",
                "status": "ok",
                "checked_event_count": 0,
                "stale_event_count": 0,
                "refresh_overdue_count": 0,
                "max_age_days": 7,
                "affected_symbols": [],
                "stale_events": [],
                "refresh_overdue": [],
            }

            paths = event_check.persist_event_freshness_report(config=config, payload=payload)

            self.assertTrue(Path(paths["json_report_path"]).exists())
            self.assertTrue(Path(paths["markdown_report_path"]).exists())


if __name__ == "__main__":
    unittest.main()
