"""Tests for the OpenAI veto-only critic client.

The non-negotiable contract being tested:

1. Returns ``severity in {none, minor, major}`` always — never any
   key that could be interpreted as a trade instruction.
2. Returns ``severity == "none"`` for every failure path (no key,
   network error, malformed JSON, daily cap reached).
3. ``major`` is the only severity that should cause the caller to
   veto. Tests do not assert what the caller does, only that the
   client surfaces severity faithfully.
"""

from __future__ import annotations

import json
import unittest
import urllib.error
from unittest.mock import patch

from ai import openai_client
from ai.openai_client import (
    _SpendTracker,
    _parse_response,
    critique_with_llm,
    reset_spend_tracker,
)


class _FakeResponse:
    def __init__(self, *, status: int = 200, body: bytes = b""):
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_a: object) -> None:
        return None


def _chat_payload(*, severity: str = "none", concerns: list | None = None,
                  prompt_tokens: int = 100, completion_tokens: int = 50) -> bytes:
    body = {
        "id": "chatcmpl-x",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps(
                        {"severity": severity, "concerns": concerns or []}
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return json.dumps(body).encode("utf-8")


_ORDER = {
    "strategy_name": "cash_secured_put",
    "underlying": "AAPL",
    "max_loss": 9000.0,
    "iv_rank": 35.0,
    "legs": [{"side": "sell_to_open", "option_type": "put", "strike": 180.0}],
}


class ParseResponseTests(unittest.TestCase):
    def test_extracts_severity_and_concerns(self):
        payload = json.loads(
            _chat_payload(severity="major", concerns=["earnings tomorrow"])
        )
        result = _parse_response(payload)
        self.assertEqual(result["severity"], "major")
        self.assertEqual(result["concerns"], ["earnings tomorrow"])
        self.assertEqual(result["source"], "openai")

    def test_unknown_severity_falls_back_to_none(self):
        payload = json.loads(
            _chat_payload(severity="catastrophic", concerns=["??"])
        )
        result = _parse_response(payload)
        self.assertEqual(result["severity"], "none")

    def test_non_string_concerns_dropped(self):
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"severity": "minor", "concerns": [1, "ok", None, ""]}
                        )
                    }
                }
            ]
        }
        result = _parse_response(body)
        self.assertEqual(result["concerns"], ["ok"])

    def test_concerns_capped_at_ten(self):
        body = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "severity": "minor",
                                "concerns": [f"c{i}" for i in range(25)],
                            }
                        )
                    }
                }
            ]
        }
        result = _parse_response(body)
        self.assertEqual(len(result["concerns"]), 10)

    def test_missing_choices_yields_none(self):
        self.assertEqual(_parse_response({})["severity"], "none")

    def test_non_json_content_yields_none(self):
        body = {"choices": [{"message": {"content": "I think this is bad."}}]}
        self.assertEqual(_parse_response(body)["severity"], "none")

    def test_content_object_must_be_dict(self):
        body = {"choices": [{"message": {"content": json.dumps([1, 2, 3])}}]}
        self.assertEqual(_parse_response(body)["severity"], "none")


class CritiqueWithLlmTests(unittest.TestCase):
    def setUp(self) -> None:
        reset_spend_tracker()

    def test_returns_none_without_api_key(self):
        with patch.object(openai_client.urllib.request, "urlopen") as urlopen, \
             patch.dict("os.environ", {}, clear=True):
            result = critique_with_llm(_ORDER)
            self.assertEqual(result["severity"], "none")
            urlopen.assert_not_called()

    def test_empty_order_returns_none(self):
        result = critique_with_llm({}, api_key="k")
        self.assertEqual(result["severity"], "none")

    def test_happy_path_returns_severity(self):
        fake = _FakeResponse(body=_chat_payload(severity="major", concerns=["earnings"]))
        with patch.object(openai_client.urllib.request, "urlopen", return_value=fake):
            result = critique_with_llm(_ORDER, api_key="sk-test")
        self.assertEqual(result["severity"], "major")
        self.assertEqual(result["concerns"], ["earnings"])

    def test_network_error_returns_none(self):
        with patch.object(
            openai_client.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("boom"),
        ):
            self.assertEqual(critique_with_llm(_ORDER, api_key="k")["severity"], "none")

    def test_non_200_returns_none(self):
        fake = _FakeResponse(status=429, body=b"rate limited")
        with patch.object(openai_client.urllib.request, "urlopen", return_value=fake):
            self.assertEqual(critique_with_llm(_ORDER, api_key="k")["severity"], "none")

    def test_invalid_json_response_returns_none(self):
        fake = _FakeResponse(body=b"not-json")
        with patch.object(openai_client.urllib.request, "urlopen", return_value=fake):
            self.assertEqual(critique_with_llm(_ORDER, api_key="k")["severity"], "none")

    def test_request_uses_bearer_auth(self):
        fake = _FakeResponse(body=_chat_payload())
        with patch.object(openai_client.urllib.request, "urlopen", return_value=fake) as urlopen:
            critique_with_llm(_ORDER, api_key="sk-abc")
        request = urlopen.call_args[0][0]
        self.assertEqual(request.get_header("Authorization"), "Bearer sk-abc")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(body["temperature"], 0.0)
        self.assertEqual(body["response_format"], {"type": "json_object"})
        # System prompt must remind the LLM that it is veto-only.
        system = body["messages"][0]["content"]
        self.assertIn("veto-only", system)

    def test_daily_spend_cap_short_circuits(self):
        tracker = _SpendTracker()
        tracker.record(10.0)  # already past a $5 cap
        with patch.object(openai_client.urllib.request, "urlopen") as urlopen:
            result = critique_with_llm(
                _ORDER,
                api_key="k",
                daily_spend_cap_usd=5.0,
                spend_tracker=tracker,
            )
        self.assertEqual(result["severity"], "none")
        urlopen.assert_not_called()

    def test_spend_is_recorded_on_success(self):
        tracker = _SpendTracker()
        # 2000 prompt + 500 completion tokens — easily measurable.
        fake = _FakeResponse(
            body=_chat_payload(prompt_tokens=2000, completion_tokens=500)
        )
        with patch.object(openai_client.urllib.request, "urlopen", return_value=fake):
            critique_with_llm(
                _ORDER,
                api_key="k",
                daily_spend_cap_usd=5.0,
                spend_tracker=tracker,
            )
        self.assertGreater(tracker.spent(), 0.0)
        self.assertLess(tracker.spent(), 5.0)

    def test_response_keys_are_safe(self):
        """Veto-only invariant: result keys never carry trade instructions."""
        fake = _FakeResponse(body=_chat_payload(severity="minor", concerns=["x"]))
        with patch.object(openai_client.urllib.request, "urlopen", return_value=fake):
            result = critique_with_llm(_ORDER, api_key="k")
        self.assertEqual(
            set(result.keys()), {"severity", "concerns", "source", "reason"}
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
