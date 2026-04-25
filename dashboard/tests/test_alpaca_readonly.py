from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

from dashboard import alpaca_readonly


class AlpacaReadOnlyTests(unittest.TestCase):
    def test_allowlist_and_denylist_do_not_overlap(self):
        self.assertEqual(alpaca_readonly.ALLOWED_FUNCTIONS & alpaca_readonly.FORBIDDEN_FUNCTIONS, set())

    def test_get_positions_returns_empty_list_on_error(self):
        with patch.object(alpaca_readonly, "get_all_positions", side_effect=RuntimeError("boom")):
            self.assertEqual(alpaca_readonly.get_positions_as_dicts(), [])

    def test_account_summary_returns_empty_dict_on_error(self):
        with patch.object(alpaca_readonly, "get_account", side_effect=RuntimeError("offline")):
            self.assertEqual(alpaca_readonly.get_account_summary(), {})

    def test_missing_dashboard_credentials_raise_clear_error(self):
        with patch.dict(os.environ, {}, clear=True), patch.object(alpaca_readonly, "_trading_client", None):
            with self.assertRaises(RuntimeError):
                alpaca_readonly._get_dashboard_credentials()

    def test_dashboard_env_uses_options_prefix(self):
        fake_client = MagicMock(name="TradingClient")
        with patch.dict(
            os.environ,
            {
                "ALPACA_OPTIONS_DASHBOARD_PAPER_API_KEY": "paper-key",
                "ALPACA_OPTIONS_DASHBOARD_PAPER_SECRET_KEY": "paper-secret",
            },
            clear=False,
        ), patch.object(alpaca_readonly, "TradingClient", return_value=fake_client), patch.object(alpaca_readonly, "_trading_client", None), patch.object(alpaca_readonly, "cfg") as mock_cfg:
            mock_cfg.return_value.mode = "paper"
            client = alpaca_readonly._get_trading_client()
        self.assertIs(client, fake_client)


if __name__ == "__main__":
    unittest.main()
