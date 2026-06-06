from __future__ import annotations

import unittest
from datetime import date

from core.event_freshness import event_data_freshness_report


class EventDataFreshnessTests(unittest.TestCase):
    def test_past_event_dates_are_flagged_and_cleared_from_sanitized_copy(self):
        underlyings = [
            {
                "symbol": "AAPL",
                "next_earnings_date": "2026-05-02",
                "ex_dividend_date": date(2026, 6, 19),
            }
        ]

        report = event_data_freshness_report(
            underlyings,
            config={"underlyings": {"refresh_earnings_dates_days": 7}},
            as_of=date(2026, 6, 6),
        )

        self.assertEqual(report["status"], "stale")
        self.assertEqual(report["stale_event_count"], 1)
        self.assertEqual(report["stale_events"][0]["reason"], "past_event_date")
        self.assertIsNone(report["sanitized_underlyings"][0]["next_earnings_date"])
        self.assertEqual(report["sanitized_underlyings"][0]["ex_dividend_date"], date(2026, 6, 19))

    def test_future_date_with_old_refresh_timestamp_is_reported_overdue_but_kept(self):
        underlyings = [
            {
                "symbol": "MSFT",
                "next_earnings_date": "2026-06-20",
                "earnings_date_updated_at": "2026-05-20T12:00:00Z",
            }
        ]

        report = event_data_freshness_report(
            underlyings,
            config={"underlyings": {"refresh_earnings_dates_days": 7}},
            as_of=date(2026, 6, 6),
        )

        self.assertEqual(report["status"], "stale")
        self.assertEqual(report["stale_event_count"], 0)
        self.assertEqual(report["refresh_overdue_count"], 1)
        self.assertEqual(report["refresh_overdue"][0]["reason"], "refresh_overdue")
        self.assertEqual(report["sanitized_underlyings"][0]["next_earnings_date"], "2026-06-20")


if __name__ == "__main__":
    unittest.main()
