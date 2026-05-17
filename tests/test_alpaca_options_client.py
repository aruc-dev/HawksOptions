from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

import core.alpaca_options_client as client_module
from core.alpaca_options_client import AlpacaOptionsClient
from core.config import load_config
from core.occ import parse_occ_symbol


class _Quote:
    def __init__(self, bid_price: float, ask_price: float):
        self.bid_price = bid_price
        self.ask_price = ask_price


class _LatestQuoteRequest:
    def __init__(self, symbol_or_symbols):
        self.symbol_or_symbols = symbol_or_symbols


class _OptionDataClient:
    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret

    def get_option_latest_quote(self, request):
        return {
            symbol: _Quote(1.2, 1.4)
            for symbol in request.symbol_or_symbols
        }


class _StockDataClient:
    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret

    def get_stock_latest_quote(self, request):
        return {
            symbol: _Quote(19.8, 20.2)
            for symbol in request.symbol_or_symbols
        }


class _FailingStockDataClient:
    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret

    def get_stock_latest_quote(self, request):
        raise TimeoutError("provider timeout")


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

    def test_live_option_quotes_use_alpaca_data_client(self):
        config = load_config()
        with (
            patch.dict("os.environ", {"ALPACA_OPTIONS_PAPER_API_KEY": "key", "ALPACA_OPTIONS_PAPER_SECRET_KEY": "secret"}),
            patch.object(client_module, "OptionHistoricalDataClient", _OptionDataClient),
            patch.object(client_module, "OptionLatestQuoteRequest", _LatestQuoteRequest),
        ):
            client = AlpacaOptionsClient(config, use_sample_data=False)
            quotes = client.get_option_quotes(["SPY260619P00500000"])

        self.assertEqual(quotes["SPY260619P00500000"]["bid"], 1.2)
        self.assertEqual(quotes["SPY260619P00500000"]["ask"], 1.4)
        self.assertEqual(quotes["SPY260619P00500000"]["source"], "alpaca_option_latest_quote")

    def test_live_market_volatility_snapshot_uses_configured_symbol_quote(self):
        config = load_config()
        config["market_data"]["vix_symbol"] = "VIX"
        with (
            patch.dict("os.environ", {"ALPACA_OPTIONS_PAPER_API_KEY": "key", "ALPACA_OPTIONS_PAPER_SECRET_KEY": "secret"}),
            patch.object(client_module, "StockHistoricalDataClient", _StockDataClient),
            patch.object(client_module, "StockLatestQuoteRequest", _LatestQuoteRequest),
        ):
            client = AlpacaOptionsClient(config, use_sample_data=False)
            snapshot = client.get_market_volatility_snapshot(as_of=date(2026, 4, 23))

        self.assertEqual(snapshot["vix"], 20.0)
        self.assertEqual(snapshot["source"], "alpaca_stock_latest_quote")
        self.assertEqual(snapshot["symbol"], "VIX")

    def test_live_market_volatility_snapshot_returns_unavailable_on_provider_error(self):
        config = load_config()
        config["market_data"]["vix_symbol"] = "VIX"
        with (
            patch.dict("os.environ", {"ALPACA_OPTIONS_PAPER_API_KEY": "key", "ALPACA_OPTIONS_PAPER_SECRET_KEY": "secret"}),
            patch.object(client_module, "StockHistoricalDataClient", _FailingStockDataClient),
            patch.object(client_module, "StockLatestQuoteRequest", _LatestQuoteRequest),
        ):
            client = AlpacaOptionsClient(config, use_sample_data=False)
            snapshot = client.get_market_volatility_snapshot(as_of=date(2026, 4, 23))

        self.assertEqual(snapshot["vix"], None)
        self.assertEqual(snapshot["source"], "alpaca_stock_latest_quote_error")
        self.assertEqual(snapshot["symbol"], "VIX")

    def test_occ_symbols_round_trip(self):
        client = AlpacaOptionsClient(load_config(), use_sample_data=True)
        contract = client.get_option_chain("SPY", as_of=date(2026, 4, 23))[0]
        parsed = parse_occ_symbol(contract.contract_symbol)
        self.assertEqual(parsed["underlying"], "SPY")


if __name__ == "__main__":
    unittest.main()
