from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import core.alpaca_options_client as client_module
from core.alpaca_options_client import AlpacaOptionsClient
from core.config import load_config
from core.occ import parse_occ_symbol


class _Quote:
    def __init__(self, bid_price: float, ask_price: float):
        self.bid_price = bid_price
        self.ask_price = ask_price
        self.timestamp = datetime.now(timezone.utc)


class _LatestQuoteRequest:
    def __init__(self, symbol_or_symbols):
        self.symbol_or_symbols = symbol_or_symbols


class _KeywordRequest:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _TimeFrame:
    Day = "1Day"


class _AssetStatus:
    ACTIVE = "active"


class _OptionDataClient:
    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret

    def get_option_latest_quote(self, request):
        return {
            symbol: _Quote(1.2, 1.4)
            for symbol in request.symbol_or_symbols
        }


class _RecordingOptionDataClient(_OptionDataClient):
    latest_quote_requests = []

    @classmethod
    def reset(cls):
        cls.latest_quote_requests = []

    def get_option_latest_quote(self, request):
        type(self).latest_quote_requests.append(request)
        return super().get_option_latest_quote(request)


class _StockDataClient:
    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret

    def get_stock_latest_quote(self, request):
        return {
            symbol: _Quote(19.8, 20.2)
            for symbol in request.symbol_or_symbols
        }


class _SpyStockDataClient:
    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret

    def get_stock_latest_quote(self, request):
        return {
            symbol: _Quote(499.8, 500.2)
            for symbol in request.symbol_or_symbols
        }


class _FailingStockDataClient:
    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret

    def get_stock_latest_quote(self, request):
        raise TimeoutError("provider timeout")


class _OptionChainDataClient:
    def __init__(self, key: str, secret: str):
        self.key = key
        self.secret = secret

    def get_option_chain(self, request):
        return {
            "SPY260619P00500000": {
                "latest_quote": _Quote(1.2, 1.4),
                "latest_trade": {"price": 1.3, "size": 25},
                "implied_volatility": 0.22,
                "greeks": {
                    "delta": -0.21,
                    "theta": -0.03,
                    "vega": 0.12,
                    "gamma": 0.01,
                },
            }
        }

    def get_option_bars(self, request):
        return {"SPY260619P00500000": [{"volume": 123}]}


class _NoDailyBarOptionChainDataClient(_OptionChainDataClient):
    def get_option_bars(self, request):
        return {}


class _CountingOptionChainDataClient(_OptionChainDataClient):
    def __init__(self, key: str, secret: str):
        super().__init__(key, secret)
        self.chain_calls = 0

    def get_option_chain(self, request):
        self.chain_calls += 1
        return super().get_option_chain(request)


class _RecordingOptionChainDataClient(_OptionChainDataClient):
    chain_requests = []
    bar_requests = []

    @classmethod
    def reset(cls):
        cls.chain_requests = []
        cls.bar_requests = []

    def get_option_chain(self, request):
        type(self).chain_requests.append(request)
        return super().get_option_chain(request)

    def get_option_bars(self, request):
        type(self).bar_requests.append(request)
        return super().get_option_bars(request)


class _TradingClient:
    def __init__(self, key: str, secret: str, paper: bool):
        self.key = key
        self.secret = secret
        self.paper = paper

    def get_option_contracts(self, request):
        return {
            "option_contracts": [
                {
                    "symbol": "SPY260619P00500000",
                    "open_interest": "850",
                    "open_interest_date": date(2026, 4, 22),
                }
            ],
            "next_page_token": None,
        }


class AlpacaOptionsClientTests(unittest.TestCase):
    def test_sample_chain_contains_options(self):
        client = AlpacaOptionsClient(load_config(), use_sample_data=True)
        chain = client.get_option_chain("SPY", as_of=date(2026, 4, 23))
        self.assertGreater(len(chain), 100)

    def test_sample_option_quotes_include_fresh_timestamp(self):
        client = AlpacaOptionsClient(load_config(), use_sample_data=True)
        symbol = client.get_option_chain("SPY", as_of=date.today())[0].contract_symbol
        before = datetime.now(timezone.utc)
        quotes = client.get_option_quotes([symbol])
        after = datetime.now(timezone.utc)
        timestamp = datetime.fromisoformat(quotes[symbol]["timestamp"])

        self.assertGreaterEqual(timestamp, before.replace(microsecond=0))
        self.assertLessEqual(timestamp, after)

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
            patch.object(client_module, "OptionChainRequest", _KeywordRequest),
        ):
            client = AlpacaOptionsClient(config, use_sample_data=False)
            quotes = client.get_option_quotes(["SPY260619P00500000"])

        self.assertEqual(quotes["SPY260619P00500000"]["bid"], 1.2)
        self.assertEqual(quotes["SPY260619P00500000"]["ask"], 1.4)
        self.assertEqual(quotes["SPY260619P00500000"]["source"], "alpaca_option_latest_quote")
        self.assertIn("timestamp", quotes["SPY260619P00500000"])

    def test_paper_option_quotes_default_to_indicative_feed_when_supported(self):
        config = load_config()
        _RecordingOptionDataClient.reset()
        with (
            patch.dict("os.environ", {"ALPACA_OPTIONS_PAPER_API_KEY": "key", "ALPACA_OPTIONS_PAPER_SECRET_KEY": "secret"}),
            patch.object(client_module, "OptionHistoricalDataClient", _RecordingOptionDataClient),
            patch.object(client_module, "OptionLatestQuoteRequest", _KeywordRequest),
            patch.object(client_module, "OptionChainRequest", _KeywordRequest),
        ):
            client = AlpacaOptionsClient(config, use_sample_data=False)
            client.get_option_quotes(["SPY260619P00500000"])

        self.assertEqual(_RecordingOptionDataClient.latest_quote_requests[0].feed, "indicative")

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

    def test_default_live_market_volatility_symbol_uses_tradable_proxy(self):
        config = load_config()

        self.assertEqual(config["market_data"]["vix_symbol"], "VIXY")
        self.assertEqual(config["market_data"]["vix_symbol_scale"], "proxy")

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

    def test_live_option_chain_uses_alpaca_chain_quotes_and_contract_metadata(self):
        config = load_config()
        config["market_data"]["use_sample_data"] = False
        with (
            patch.dict("os.environ", {"ALPACA_OPTIONS_PAPER_API_KEY": "key", "ALPACA_OPTIONS_PAPER_SECRET_KEY": "secret"}),
            patch.object(client_module, "TradingClient", _TradingClient),
            patch.object(client_module, "OptionHistoricalDataClient", _OptionChainDataClient),
            patch.object(client_module, "OptionLatestQuoteRequest", _LatestQuoteRequest),
            patch.object(client_module, "OptionChainRequest", _KeywordRequest),
            patch.object(client_module, "OptionBarsRequest", _KeywordRequest),
            patch.object(client_module, "TimeFrame", _TimeFrame),
            patch.object(client_module, "StockHistoricalDataClient", _SpyStockDataClient),
            patch.object(client_module, "StockLatestQuoteRequest", _LatestQuoteRequest),
            patch.object(client_module, "GetOptionContractsRequest", _KeywordRequest),
            patch.object(client_module, "AssetStatus", _AssetStatus),
        ):
            client = AlpacaOptionsClient(config, use_sample_data=False)
            chain = client.get_option_chain("SPY", as_of=date(2026, 4, 23))

        self.assertEqual(len(chain), 1)
        contract = chain[0]
        self.assertEqual(contract.contract_symbol, "SPY260619P00500000")
        self.assertEqual(contract.option_type, "put")
        self.assertEqual(contract.open_interest, 850)
        self.assertEqual(contract.volume, 123)
        self.assertEqual(contract.delta, -0.21)
        self.assertEqual(contract.underlying_price, 500.0)
        self.assertEqual(contract.meta["source"], "alpaca_option_chain")
        self.assertEqual(contract.meta["volume_source"], "daily_bar")

    def test_paper_option_chain_defaults_to_indicative_and_omits_current_bar_end(self):
        config = load_config()
        config["market_data"]["use_sample_data"] = False
        _RecordingOptionChainDataClient.reset()
        with (
            patch.dict("os.environ", {"ALPACA_OPTIONS_PAPER_API_KEY": "key", "ALPACA_OPTIONS_PAPER_SECRET_KEY": "secret"}),
            patch.object(client_module, "TradingClient", _TradingClient),
            patch.object(client_module, "OptionHistoricalDataClient", _RecordingOptionChainDataClient),
            patch.object(client_module, "OptionLatestQuoteRequest", _KeywordRequest),
            patch.object(client_module, "OptionChainRequest", _KeywordRequest),
            patch.object(client_module, "OptionBarsRequest", _KeywordRequest),
            patch.object(client_module, "TimeFrame", _TimeFrame),
            patch.object(client_module, "StockHistoricalDataClient", _SpyStockDataClient),
            patch.object(client_module, "StockLatestQuoteRequest", _LatestQuoteRequest),
            patch.object(client_module, "GetOptionContractsRequest", _KeywordRequest),
            patch.object(client_module, "AssetStatus", _AssetStatus),
        ):
            client = AlpacaOptionsClient(config, use_sample_data=False)
            client.get_option_chain("SPY", as_of=datetime.now(timezone.utc).date())

        self.assertEqual(_RecordingOptionChainDataClient.chain_requests[0].feed, "indicative")
        self.assertEqual(_RecordingOptionChainDataClient.bar_requests[0].feed, "indicative")
        self.assertIsNone(getattr(_RecordingOptionChainDataClient.bar_requests[0], "end", None))

    def test_live_option_chain_does_not_default_to_indicative_feed(self):
        config = load_config()
        config["mode"] = "live"
        config["market_data"]["use_sample_data"] = False
        _RecordingOptionChainDataClient.reset()
        with (
            patch.dict("os.environ", {"ALPACA_OPTIONS_LIVE_API_KEY": "key", "ALPACA_OPTIONS_LIVE_SECRET_KEY": "secret"}),
            patch.object(client_module, "TradingClient", _TradingClient),
            patch.object(client_module, "OptionHistoricalDataClient", _RecordingOptionChainDataClient),
            patch.object(client_module, "OptionLatestQuoteRequest", _KeywordRequest),
            patch.object(client_module, "OptionChainRequest", _KeywordRequest),
            patch.object(client_module, "OptionBarsRequest", _KeywordRequest),
            patch.object(client_module, "TimeFrame", _TimeFrame),
            patch.object(client_module, "StockHistoricalDataClient", _SpyStockDataClient),
            patch.object(client_module, "StockLatestQuoteRequest", _LatestQuoteRequest),
            patch.object(client_module, "GetOptionContractsRequest", _KeywordRequest),
            patch.object(client_module, "AssetStatus", _AssetStatus),
        ):
            client = AlpacaOptionsClient(config, use_sample_data=False)
            client.get_option_chain("SPY", as_of=datetime.now(timezone.utc).date())

        self.assertFalse(hasattr(_RecordingOptionChainDataClient.chain_requests[0], "feed"))
        self.assertFalse(hasattr(_RecordingOptionChainDataClient.bar_requests[0], "feed"))
        self.assertIsNotNone(getattr(_RecordingOptionChainDataClient.bar_requests[0], "end", None))

    def test_live_underlying_snapshot_derives_iv_from_option_chain(self):
        config = load_config()
        config["market_data"]["use_sample_data"] = False
        with TemporaryDirectory() as tmp:
            config["reporting"]["iv_history_file"] = str(Path(tmp) / "missing_iv_history.csv")
            with (
                patch.dict("os.environ", {"ALPACA_OPTIONS_PAPER_API_KEY": "key", "ALPACA_OPTIONS_PAPER_SECRET_KEY": "secret"}),
                patch.object(client_module, "OptionHistoricalDataClient", _OptionChainDataClient),
                patch.object(client_module, "OptionLatestQuoteRequest", _LatestQuoteRequest),
                patch.object(client_module, "OptionChainRequest", _KeywordRequest),
                patch.object(client_module, "StockHistoricalDataClient", _SpyStockDataClient),
                patch.object(client_module, "StockLatestQuoteRequest", _LatestQuoteRequest),
            ):
                client = AlpacaOptionsClient(config, use_sample_data=False)
                snapshot = client.get_underlying_snapshot("SPY", as_of=date(2026, 4, 23))

        self.assertEqual(snapshot["price"], 500.0)
        self.assertEqual(snapshot["current_iv"], 0.22)
        self.assertEqual(snapshot["iv_rank"], 50.0)
        self.assertEqual(snapshot["iv_percentile"], 50.0)
        self.assertEqual(snapshot["iv_source"], "alpaca_option_chain")
        self.assertEqual(snapshot["iv_rank_source"], "neutral_no_history")

    def test_live_option_chain_reuses_chain_response_for_iv_snapshot(self):
        config = load_config()
        config["market_data"]["use_sample_data"] = False
        with TemporaryDirectory() as tmp:
            config["reporting"]["iv_history_file"] = str(Path(tmp) / "missing_iv_history.csv")
            with (
                patch.dict("os.environ", {"ALPACA_OPTIONS_PAPER_API_KEY": "key", "ALPACA_OPTIONS_PAPER_SECRET_KEY": "secret"}),
                patch.object(client_module, "TradingClient", _TradingClient),
                patch.object(client_module, "OptionHistoricalDataClient", _CountingOptionChainDataClient),
                patch.object(client_module, "OptionLatestQuoteRequest", _LatestQuoteRequest),
                patch.object(client_module, "OptionChainRequest", _KeywordRequest),
                patch.object(client_module, "OptionBarsRequest", _KeywordRequest),
                patch.object(client_module, "TimeFrame", _TimeFrame),
                patch.object(client_module, "StockHistoricalDataClient", _SpyStockDataClient),
                patch.object(client_module, "StockLatestQuoteRequest", _LatestQuoteRequest),
                patch.object(client_module, "GetOptionContractsRequest", _KeywordRequest),
                patch.object(client_module, "AssetStatus", _AssetStatus),
            ):
                client = AlpacaOptionsClient(config, use_sample_data=False)
                chain = client.get_option_chain("SPY", as_of=date(2026, 4, 23))
                option_data_client = client._option_data_client

        self.assertEqual(len(chain), 1)
        self.assertEqual(option_data_client.chain_calls, 1)

    def test_live_option_chain_does_not_treat_latest_trade_size_as_daily_volume(self):
        config = load_config()
        config["market_data"]["use_sample_data"] = False
        with (
            patch.dict("os.environ", {"ALPACA_OPTIONS_PAPER_API_KEY": "key", "ALPACA_OPTIONS_PAPER_SECRET_KEY": "secret"}),
            patch.object(client_module, "TradingClient", _TradingClient),
            patch.object(client_module, "OptionHistoricalDataClient", _NoDailyBarOptionChainDataClient),
            patch.object(client_module, "OptionLatestQuoteRequest", _LatestQuoteRequest),
            patch.object(client_module, "OptionChainRequest", _KeywordRequest),
            patch.object(client_module, "OptionBarsRequest", _KeywordRequest),
            patch.object(client_module, "TimeFrame", _TimeFrame),
            patch.object(client_module, "StockHistoricalDataClient", _SpyStockDataClient),
            patch.object(client_module, "StockLatestQuoteRequest", _LatestQuoteRequest),
            patch.object(client_module, "GetOptionContractsRequest", _KeywordRequest),
            patch.object(client_module, "AssetStatus", _AssetStatus),
        ):
            client = AlpacaOptionsClient(config, use_sample_data=False)
            chain = client.get_option_chain("SPY", as_of=date(2026, 4, 23))

        self.assertEqual(chain[0].volume, 0)
        self.assertEqual(chain[0].meta["volume_source"], "unavailable")

    def test_default_live_chain_strike_window_covers_far_wings(self):
        client = AlpacaOptionsClient(load_config(), use_sample_data=False)

        self.assertEqual(client._live_chain_strike_bounds(100.0), (80.0, 120.0))

    def test_occ_symbols_round_trip(self):
        client = AlpacaOptionsClient(load_config(), use_sample_data=True)
        contract = client.get_option_chain("SPY", as_of=date(2026, 4, 23))[0]
        parsed = parse_occ_symbol(contract.contract_symbol)
        self.assertEqual(parsed["underlying"], "SPY")


if __name__ == "__main__":
    unittest.main()
