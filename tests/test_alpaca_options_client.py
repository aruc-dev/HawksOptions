from __future__ import annotations

import unittest
from datetime import date

from core.alpaca_options_client import AlpacaOptionsClient
from core.config import load_config
from core.occ import parse_occ_symbol


class AlpacaOptionsClientTests(unittest.TestCase):
    def test_sample_chain_contains_options(self):
        client = AlpacaOptionsClient(load_config(), use_sample_data=True)
        chain = client.get_option_chain("SPY", as_of=date(2026, 4, 23))
        self.assertGreater(len(chain), 100)

    def test_sample_snapshot_includes_iv_percentile(self):
        client = AlpacaOptionsClient(load_config(), use_sample_data=True)
        snapshot = client.get_underlying_snapshot("SPY", as_of=date(2026, 4, 23))

        self.assertIn("iv_rank", snapshot)
        self.assertIn("iv_percentile", snapshot)
        self.assertGreaterEqual(snapshot["iv_percentile"], 0.0)
        self.assertLessEqual(snapshot["iv_percentile"], 100.0)

    def test_occ_symbols_round_trip(self):
        client = AlpacaOptionsClient(load_config(), use_sample_data=True)
        contract = client.get_option_chain("SPY", as_of=date(2026, 4, 23))[0]
        parsed = parse_occ_symbol(contract.contract_symbol)
        self.assertEqual(parsed["underlying"], "SPY")


if __name__ == "__main__":
    unittest.main()
