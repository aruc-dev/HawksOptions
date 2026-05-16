"""HTTP client for an OpenAI-backed trade critic.

The deterministic critic in ``ai/trade_idea_critic.py`` evaluates the
*structure* of a candidate order. This module adds an optional LLM
second-opinion that can read prose context (news headlines, earnings
proximity, dividend timing) and raise a veto on a major concern that
the structural checks would not catch.

Non-negotiable invariants — see CLAUDE.md:

1. **Veto-only.** The LLM may return ``severity == "major"``,
   ``"minor"``, or ``"none"``. It can never originate a trade,
   upsize, or relax a major concern raised by the deterministic
   critic.
2. **Fail closed against veto.** Any error (missing key, network
   failure, malformed JSON, daily-cap reached) returns
   ``severity == "none"`` so the call cannot block trading on its own
   infrastructure problems.
3. **Bounded spend.** ``daily_spend_cap_usd`` from config is honored
   via an in-memory token estimate. Once reached, further calls
   short-circuit to ``severity == "none"``.

Stdlib-only HTTP keeps the dependency surface unchanged. Tests mock
``urllib.request.urlopen``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from typing import Any

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_MODEL = "gpt-4o-mini"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_MAX_TOKENS = 400

# Rough cost estimates ($/1K tokens) for the default model. Used only
# for the daily-spend cap; not authoritative billing.
_PRICE_INPUT_PER_1K = 0.00015
_PRICE_OUTPUT_PER_1K = 0.0006

_LOGGER = logging.getLogger(__name__)

_VALID_SEVERITIES = {"none", "minor", "major"}
AI_REVIEW_RESULT_KEYS = frozenset({"severity", "concerns", "source", "reason"})


_SYSTEM_PROMPT = (
    "You are a veto-only options-trade reviewer. You never originate "
    "trades, never recommend upsizing, and never relax risk. Given a "
    "candidate options order and surrounding context, return strict "
    "JSON with keys: severity (one of 'none', 'minor', 'major'), and "
    "concerns (a list of short strings). Use 'major' only for a "
    "concrete, material risk such as imminent earnings inside the "
    "trade, an undisclosed corporate action, or a clear structural "
    "flaw. Use 'minor' for soft warnings. Use 'none' otherwise. "
    "Output JSON only — no prose, no markdown."
)


class _SpendTracker:
    """Per-process estimator of OpenAI cost. Resets at UTC date change."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._date: date | None = None
        self._spent_usd: float = 0.0

    def _roll_if_new_day(self) -> None:
        today = datetime.now(timezone.utc).date()
        if self._date != today:
            self._date = today
            self._spent_usd = 0.0

    def can_spend(self, cap_usd: float) -> bool:
        with self._lock:
            self._roll_if_new_day()
            return self._spent_usd < float(cap_usd)

    def record(self, usd: float) -> None:
        with self._lock:
            self._roll_if_new_day()
            self._spent_usd += max(0.0, float(usd))

    def spent(self) -> float:
        with self._lock:
            self._roll_if_new_day()
            return self._spent_usd

    def reset(self) -> None:
        with self._lock:
            self._date = datetime.now(timezone.utc).date()
            self._spent_usd = 0.0


_SPEND = _SpendTracker()


def _api_key() -> str:
    return (os.environ.get("OPENAI_API_KEY") or "").strip()


def _none_result(reason: str = "") -> dict[str, Any]:
    """Standard 'no veto' response, used by every fail-closed path."""
    return {"severity": "none", "concerns": [], "source": "openai", "reason": reason}


def safe_review_result(payload: dict[str, Any] | None, *, source: str = "openai") -> dict[str, Any]:
    """Return the only AI fields allowed to leave the review boundary."""
    payload = payload if isinstance(payload, dict) else {}
    return {
        "severity": _normalise_severity(payload.get("severity")),
        "concerns": _normalise_concerns(payload.get("concerns")),
        "source": str(payload.get("source") or source),
        "reason": str(payload.get("reason") or ""),
    }


def _estimate_cost_usd(usage: dict[str, Any] | None) -> float:
    if not isinstance(usage, dict):
        return 0.0
    prompt_tokens = float(usage.get("prompt_tokens", 0) or 0)
    completion_tokens = float(usage.get("completion_tokens", 0) or 0)
    return (
        (prompt_tokens / 1000.0) * _PRICE_INPUT_PER_1K
        + (completion_tokens / 1000.0) * _PRICE_OUTPUT_PER_1K
    )


def _normalise_severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in _VALID_SEVERITIES:
        return text
    return "none"


def _normalise_concerns(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    cleaned: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip())
    if len(cleaned) > 10:
        cleaned = cleaned[:10]
    return cleaned


def _parse_response(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract a {severity, concerns} dict from an OpenAI chat response.

    Any deviation from the expected shape returns the no-veto default.
    """
    if not isinstance(payload, dict):
        return _none_result("malformed payload")
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return _none_result("no choices")
    first = choices[0]
    if not isinstance(first, dict):
        return _none_result("malformed choice")
    message = first.get("message")
    if not isinstance(message, dict):
        return _none_result("missing message")
    content = message.get("content")
    if not isinstance(content, str):
        return _none_result("missing content")
    try:
        body = json.loads(content)
    except (ValueError, TypeError):
        return _none_result("non-JSON content")
    if not isinstance(body, dict):
        return _none_result("content not an object")
    return safe_review_result(body, source="openai")


def _build_user_prompt(order_summary: dict[str, Any], headlines: list[str]) -> str:
    """Compact, structured prompt — keeps token use low."""
    lines = [
        "Order:",
        json.dumps(order_summary, default=str, sort_keys=True),
    ]
    if headlines:
        lines.append("Recent headlines:")
        for headline in headlines[:10]:
            lines.append(f"- {headline}")
    lines.append('Reply with JSON: {"severity": "...", "concerns": [...]}')
    return "\n".join(lines)


def critique_with_llm(
    order_summary: dict[str, Any],
    *,
    headlines: list[str] | None = None,
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    daily_spend_cap_usd: float = 5.0,
    spend_tracker: _SpendTracker | None = None,
) -> dict[str, Any]:
    """Ask an LLM to flag major concerns about a candidate order.

    Returns ``{"severity", "concerns", "source", "reason"}``.
    Severity is one of ``"none" | "minor" | "major"``. ``"none"`` is
    returned for any failure mode (missing key, network error, daily
    cap reached, malformed response) — the caller must treat absence
    of veto as the default.
    """
    if not isinstance(order_summary, dict) or not order_summary:
        return _none_result("empty order summary")

    key = api_key if api_key is not None else _api_key()
    if not key:
        return _none_result("no api key")

    tracker = spend_tracker or _SPEND
    if not tracker.can_spend(daily_spend_cap_usd):
        return _none_result("daily spend cap reached")

    prompt = _build_user_prompt(order_summary, list(headlines or []))
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.0,
        "max_tokens": int(max_tokens),
        "response_format": {"type": "json_object"},
    }

    request = urllib.request.Request(
        OPENAI_API_URL,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "HawksOptions/1.0 (+ai.openai_client)",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            status = getattr(response, "status", 200)
            if status != 200:
                _LOGGER.warning("openai non-200 status: %s", status)
                return _none_result(f"http {status}")
            raw = response.read()
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
        _LOGGER.warning("openai request failed: %s", exc)
        return _none_result("network error")

    try:
        payload = json.loads(raw)
    except (ValueError, TypeError) as exc:
        _LOGGER.warning("openai response was not valid JSON: %s", exc)
        return _none_result("invalid json")

    cost = _estimate_cost_usd(payload.get("usage"))
    if cost > 0:
        tracker.record(cost)

    return _parse_response(payload)


def reset_spend_tracker() -> None:
    """Test hook: zero the global daily-spend tracker."""
    _SPEND.reset()
