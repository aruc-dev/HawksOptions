"""Tests for the NewsAPI client.

The client is fail-open by design — anything that prevents a real
fetch must return ``[]`` so the keyword news gate sees no risk hits
and does not veto. These tests pin that contract.
"""

from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from ai import news_client
from ai.news_client import _build_url, _parse_headlines, fetch_headlines


class _FakeResponse:
    """Mimics the urlopen context manager well enough for the client."""

    def __init__(self, *, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def _ok_payload(*titles: str) -> bytes:
    return json.dumps(
        {
            "status": "ok",
            "totalResults": len(titles),
            "articles": [{"title": t} for t in titles],
        }
    ).encode("utf-8")


class BuildUrlTests(unittest.TestCase):
    def test_url_has_required_query_params(self):
        url = _build_url("AAPL", page_size=5)
        self.assertIn("q=AAPL", url)
        self.assertIn("language=en", url)
        self.assertIn("sortBy=publishedAt", url)
        self.assertIn("pageSize=5", url)

    def test_url_escapes_symbol(self):
        # Defensive — symbol arrives unsanitised from config.
        url = _build_url("BRK B", page_size=10)
        self.assertIn("BRK+B", url)


class ParseHeadlinesTests(unittest.TestCase):
    def test_extracts_titles(self):
        payload = json.loads(_ok_payload("Apple beats earnings", "Apple sued by FTC"))
        self.assertEqual(
            _parse_headlines(payload),
            ["Apple beats earnings", "Apple sued by FTC"],
        )

    def test_skips_blank_titles(self):
        payload = {"status": "ok", "articles": [{"title": ""}, {"title": "real"}]}
        self.assertEqual(_parse_headlines(payload), ["real"])

    def test_error_status_yields_empty(self):
        payload = {"status": "error", "code": "rateLimited"}
        self.assertEqual(_parse_headlines(payload), [])

    def test_missing_articles_yields_empty(self):
        self.assertEqual(_parse_headlines({"status": "ok"}), [])

    def test_non_dict_yields_empty(self):
        self.assertEqual(_parse_headlines([]), [])  # type: ignore[arg-type]


class FetchHeadlinesTests(unittest.TestCase):
    def test_returns_titles_on_success(self):
        fake = _FakeResponse(body=_ok_payload("AAPL hits new high"))
        with patch.object(news_client.urllib.request, "urlopen", return_value=fake):
            headlines = fetch_headlines("AAPL", api_key="test-key")
        self.assertEqual(headlines, ["AAPL hits new high"])

    def test_empty_symbol_short_circuits(self):
        with patch.object(news_client.urllib.request, "urlopen") as urlopen:
            self.assertEqual(fetch_headlines("", api_key="key"), [])
            urlopen.assert_not_called()

    def test_missing_key_short_circuits(self):
        # No key in environment, none passed.
        with patch.object(news_client.urllib.request, "urlopen") as urlopen, \
             patch.dict("os.environ", {}, clear=True):
            self.assertEqual(fetch_headlines("AAPL"), [])
            urlopen.assert_not_called()

    def test_explicit_key_overrides_env(self):
        with patch.object(news_client.urllib.request, "urlopen") as urlopen, \
             patch.dict("os.environ", {"NEWS_API_KEY": "env-key"}, clear=True):
            urlopen.return_value = _FakeResponse(body=_ok_payload("hit"))
            fetch_headlines("AAPL", api_key="explicit-key")
            request = urlopen.call_args[0][0]
            self.assertEqual(request.get_header("X-api-key"), "explicit-key")

    def test_non_200_returns_empty(self):
        fake = _FakeResponse(status=403, body=b"")
        with patch.object(news_client.urllib.request, "urlopen", return_value=fake):
            self.assertEqual(fetch_headlines("AAPL", api_key="k"), [])

    def test_url_error_returns_empty(self):
        with patch.object(
            news_client.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("boom"),
        ):
            self.assertEqual(fetch_headlines("AAPL", api_key="k"), [])

    def test_timeout_returns_empty(self):
        with patch.object(
            news_client.urllib.request,
            "urlopen",
            side_effect=TimeoutError("slow"),
        ):
            self.assertEqual(fetch_headlines("AAPL", api_key="k"), [])

    def test_invalid_json_returns_empty(self):
        fake = _FakeResponse(body=b"<html>not json</html>")
        with patch.object(news_client.urllib.request, "urlopen", return_value=fake):
            self.assertEqual(fetch_headlines("AAPL", api_key="k"), [])

    def test_request_carries_api_key_header(self):
        fake = _FakeResponse(body=_ok_payload())
        with patch.object(news_client.urllib.request, "urlopen", return_value=fake) as urlopen:
            fetch_headlines("AAPL", api_key="abc123")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_header("X-api-key"), "abc123")
        self.assertEqual(request.get_header("Accept"), "application/json")

    def test_page_size_clamped(self):
        fake = _FakeResponse(body=_ok_payload())
        with patch.object(news_client.urllib.request, "urlopen", return_value=fake) as urlopen:
            fetch_headlines("AAPL", api_key="k", page_size=999)
        request = urlopen.call_args[0][0]
        # Should be clamped to MAX_PAGE_SIZE = 50.
        self.assertIn("pageSize=50", request.full_url)
        # And not pass through the absurd value.
        self.assertNotIn("pageSize=999", request.full_url)

    def test_buffered_io_response(self):
        # Some intermediaries return a BytesIO-like — make sure read() path holds.
        fake = io.BytesIO(_ok_payload("buffered title"))
        fake.status = 200  # type: ignore[attr-defined]
        # urlopen result is used as a context manager — wrap.

        class _CM:
            def __enter__(self_inner):
                return fake

            def __exit__(self_inner, *_a):
                return None

        with patch.object(news_client.urllib.request, "urlopen", return_value=_CM()):
            self.assertEqual(
                fetch_headlines("AAPL", api_key="k"),
                ["buffered title"],
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
