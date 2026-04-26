"""HTTP client for fetching headlines from newsapi.org.

This module is the network-facing companion to ``ai/news_gate.py``.
The gate evaluates a list of headlines against a deterministic keyword
set; this client supplies those headlines when ``NEWS_API_KEY`` is
present and ``ai.news_gate.enabled`` is true.

Design rules:

* **Stdlib only.** No third-party HTTP libraries — keeps the runtime
  dependency surface unchanged for an optional, veto-only feature.
* **Fail closed for vetoes, fail open for fetching.** Any error
  (missing key, network failure, malformed JSON, non-200 response) is
  swallowed and returns an empty list. Returning ``[]`` means the
  keyword gate sees no risk hits and does not veto, matching the
  philosophy that AI is opt-in and never tightens risk on its own
  failures.
* **Bounded.** Hard timeout, hard headline count, no retries.

Real network calls happen only when ``fetch_headlines`` is called with
a real key and the host is reachable; tests mock
``urllib.request.urlopen`` so no live traffic is generated under CI.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

NEWS_API_URL = "https://newsapi.org/v2/everything"
DEFAULT_TIMEOUT_SECONDS = 5.0
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50

_LOGGER = logging.getLogger(__name__)


def _api_key() -> str:
    """Return the configured NewsAPI key, or an empty string."""
    return (os.environ.get("NEWS_API_KEY") or "").strip()


def _build_url(symbol: str, page_size: int) -> str:
    query = urllib.parse.urlencode(
        {
            "q": symbol,
            "language": "en",
            "sortBy": "publishedAt",
            "pageSize": str(page_size),
        }
    )
    return f"{NEWS_API_URL}?{query}"


def _parse_headlines(payload: dict[str, Any]) -> list[str]:
    """Extract headline strings from a NewsAPI 'everything' response.

    NewsAPI returns ``{"status": "ok", "articles": [{"title": ...}]}``.
    Any other shape — including ``{"status": "error", ...}`` — yields
    an empty list so the keyword gate sees no risk hits.
    """
    if not isinstance(payload, dict):
        return []
    if str(payload.get("status", "")).lower() != "ok":
        return []
    articles = payload.get("articles")
    if not isinstance(articles, list):
        return []
    headlines: list[str] = []
    for article in articles:
        if not isinstance(article, dict):
            continue
        title = article.get("title")
        if isinstance(title, str) and title.strip():
            headlines.append(title.strip())
    return headlines


def fetch_headlines(
    symbol: str,
    *,
    api_key: str | None = None,
    page_size: int = DEFAULT_PAGE_SIZE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> list[str]:
    """Return recent English headlines mentioning ``symbol``.

    Returns an empty list when:
    * ``symbol`` is empty,
    * no API key is configured,
    * the HTTP call fails for any reason,
    * the response shape is unexpected.

    Failing open (returning ``[]``) is intentional — see module
    docstring. The keyword news gate must never veto a trade because
    the news provider is unreachable.
    """
    symbol = (symbol or "").strip()
    if not symbol:
        return []

    key = api_key if api_key is not None else _api_key()
    if not key:
        return []

    page_size = max(1, min(int(page_size), MAX_PAGE_SIZE))
    url = _build_url(symbol, page_size)
    request = urllib.request.Request(
        url,
        headers={
            "X-Api-Key": key,
            "User-Agent": "HawksOptions/1.0 (+ai.news_client)",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            status = getattr(response, "status", 200)
            if status != 200:
                _LOGGER.warning("newsapi non-200 status: %s", status)
                return []
            body = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        _LOGGER.warning("newsapi request failed: %s", exc)
        return []

    try:
        payload = json.loads(body)
    except (ValueError, TypeError) as exc:
        _LOGGER.warning("newsapi response was not valid JSON: %s", exc)
        return []

    return _parse_headlines(payload)
