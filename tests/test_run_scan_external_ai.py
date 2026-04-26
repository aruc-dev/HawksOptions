"""Tests for the external-AI wiring in scheduler.run_scan.

``evaluate_external_ai`` is the unit under test: it composes the news
gate (with optional NewsAPI fetch) and the optional OpenAI critic.
The deterministic structural critic is tested separately.
"""

from __future__ import annotations

import unittest
from datetime import date

from core.models import OptionContract, OrderLeg, StrategyOrder
from scheduler.run_scan import evaluate_external_ai


def _csp_order(*, iv_rank: float = 40.0, underlying: str = "AAPL") -> StrategyOrder:
    contract = OptionContract(
        contract_symbol="AAPL260619P00180000",
        underlying=underlying,
        option_type="put",
        strike=180.0,
        expiration=date(2026, 6, 19),
        bid=1.15,
        ask=1.25,
        delta=-0.20,
        theta=-0.05,
        vega=0.10,
        underlying_price=200.0,
    )
    return StrategyOrder(
        strategy_name="cash_secured_put",
        strategy_id="csp-1",
        underlying=underlying,
        legs=[OrderLeg(contract=contract, side="sell_to_open", qty=1)],
        max_loss=18000.0,
        max_profit=120.0,
        required_buying_power=18000.0,
        profit_take_pct=0.5,
        loss_stop_multiple=2.0,
        roll_threshold_delta=-0.40,
        iv_rank=iv_rank,
    )


class NewsGateWiringTests(unittest.TestCase):
    def test_disabled_news_gate_no_call(self):
        config = {"ai": {"enabled": False, "news_gate": {"enabled": False}}}
        called = []

        def fake_fetcher(symbol, **_kw):
            called.append(symbol)
            return []

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            fetcher=fake_fetcher,
            llm=lambda *_a, **_kw: {"severity": "none", "concerns": []},
            env={},
        )
        self.assertEqual(result["veto_reason"], "")
        self.assertEqual(called, [])

    def test_news_gate_enabled_without_key_uses_empty_headlines(self):
        # Without NEWS_API_KEY, the gate runs against [] — which never vetoes.
        config = {"ai": {"enabled": False, "news_gate": {"enabled": True}}}
        fetcher_called = []

        def fake_fetcher(symbol, **_kw):
            fetcher_called.append(symbol)
            return ["should not be used"]

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            fetcher=fake_fetcher,
            llm=lambda *_a, **_kw: {"severity": "none", "concerns": []},
            env={},  # no NEWS_API_KEY
        )
        self.assertEqual(result["veto_reason"], "")
        self.assertEqual(fetcher_called, [])
        # News gate ran (with empty headlines) and reported no veto.
        self.assertIsNotNone(result["news"])
        self.assertFalse(result["news"]["veto"])

    def test_news_gate_vetoes_on_risk_keywords(self):
        config = {"ai": {"enabled": False, "news_gate": {"enabled": True}}}

        def fake_fetcher(symbol, **_kw):
            return [
                "AAPL hit with FDA investigation",
                "Apple lawsuit and fraud probe widens",
                "CEO resigns abruptly",
            ]

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            fetcher=fake_fetcher,
            llm=lambda *_a, **_kw: {"severity": "none", "concerns": []},
            env={"NEWS_API_KEY": "test-key"},
        )
        self.assertTrue(result["veto_reason"].startswith("news_gate:"))
        self.assertEqual(
            result["news"]["matched_headlines"],
            [
                "AAPL hit with FDA investigation",
                "Apple lawsuit and fraud probe widens",
                "CEO resigns abruptly",
            ],
        )


class LlmCriticWiringTests(unittest.TestCase):
    def _enabled_config(self) -> dict:
        return {
            "ai": {
                "enabled": True,
                "provider": "openai",
                "daily_spend_cap_usd": 5.0,
                "news_gate": {"enabled": False},
                "trade_idea_critic": {"enabled": True, "veto_on": "major_concern"},
            }
        }

    def test_llm_disabled_when_ai_disabled(self):
        config = self._enabled_config()
        config["ai"]["enabled"] = False
        called = []

        def fake_llm(*_a, **_kw):
            called.append(True)
            return {"severity": "major", "concerns": ["x"]}

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            llm=fake_llm,
            env={"OPENAI_API_KEY": "sk-test"},
        )
        self.assertEqual(result["veto_reason"], "")
        self.assertEqual(called, [])

    def test_llm_skipped_when_provider_not_openai(self):
        config = self._enabled_config()
        config["ai"]["provider"] = "anthropic"
        called = []

        def fake_llm(*_a, **_kw):
            called.append(True)
            return {"severity": "major", "concerns": ["x"]}

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            llm=fake_llm,
            env={"OPENAI_API_KEY": "sk-test"},
        )
        self.assertEqual(called, [])
        self.assertEqual(result["veto_reason"], "")

    def test_llm_skipped_without_api_key(self):
        config = self._enabled_config()
        called = []

        def fake_llm(*_a, **_kw):
            called.append(True)
            return {"severity": "major", "concerns": ["x"]}

        evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            llm=fake_llm,
            env={},
        )
        self.assertEqual(called, [])

    def test_llm_major_severity_vetoes(self):
        config = self._enabled_config()

        def fake_llm(*_a, **_kw):
            return {"severity": "major", "concerns": ["earnings tomorrow"]}

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            llm=fake_llm,
            env={"OPENAI_API_KEY": "sk-test"},
        )
        self.assertTrue(result["veto_reason"].startswith("llm_critic:"))
        self.assertIn("earnings tomorrow", result["veto_reason"])

    def test_llm_receives_matched_news_headlines(self):
        config = self._enabled_config()
        config["ai"]["news_gate"]["enabled"] = True
        captured = {}

        def fake_fetcher(symbol, **_kw):
            return [
                f"{symbol} FDA investigation expands",
                f"{symbol} announces product launch",
            ]

        def fake_llm(_order, **kw):
            captured["headlines"] = kw["headlines"]
            return {"severity": "none", "concerns": []}

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            fetcher=fake_fetcher,
            llm=fake_llm,
            env={"NEWS_API_KEY": "news-key", "OPENAI_API_KEY": "sk-test"},
        )
        self.assertEqual(result["veto_reason"], "")
        self.assertEqual(captured["headlines"], ["AAPL FDA investigation expands"])

    def test_llm_minor_does_not_veto(self):
        config = self._enabled_config()

        def fake_llm(*_a, **_kw):
            return {"severity": "minor", "concerns": ["wide spread"]}

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            llm=fake_llm,
            env={"OPENAI_API_KEY": "sk-test"},
        )
        self.assertEqual(result["veto_reason"], "")
        self.assertEqual(result["llm"]["severity"], "minor")

    def test_llm_none_does_not_veto(self):
        config = self._enabled_config()

        def fake_llm(*_a, **_kw):
            return {"severity": "none", "concerns": []}

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            llm=fake_llm,
            env={"OPENAI_API_KEY": "sk-test"},
        )
        self.assertEqual(result["veto_reason"], "")

    def test_llm_skipped_when_critic_not_configured_for_veto(self):
        config = self._enabled_config()
        config["ai"]["trade_idea_critic"]["veto_on"] = "never"
        called = []

        def fake_llm(*_a, **_kw):
            called.append(True)
            return {"severity": "major", "concerns": ["x"]}

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="none",
            llm=fake_llm,
            env={"OPENAI_API_KEY": "sk-test"},
        )
        self.assertEqual(called, [])
        self.assertEqual(result["veto_reason"], "")


class VetoOnlyInvariantTests(unittest.TestCase):
    """The external AI may add veto reasons but never relax existing ones."""

    def test_external_does_not_clear_structural_severity_in_caller_state(self):
        # This function only returns a veto_reason; it doesn't mutate
        # the order. The caller in scan_market is what attaches reasons
        # to ai_veto_reason. We assert here that the function does not
        # try to signal "clear severity" — i.e., a clean LLM result on
        # a structurally-major order still returns veto_reason="".
        config = {
            "ai": {
                "enabled": True,
                "provider": "openai",
                "daily_spend_cap_usd": 5.0,
                "news_gate": {"enabled": False},
                "trade_idea_critic": {"enabled": True, "veto_on": "major_concern"},
            }
        }

        called = []

        def fake_llm(*_a, **_kw):
            called.append("llm")
            return {"severity": "none", "concerns": []}

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="major",  # caller already vetoed structurally
            llm=fake_llm,
            env={"OPENAI_API_KEY": "sk-test"},
        )
        # Returning veto_reason="" is correct — the caller already has
        # ai_veto_reason set from the structural critic and the wiring
        # in scan_market preserves it.
        self.assertEqual(result["veto_reason"], "")
        self.assertEqual(called, [])

    def test_structural_major_skips_news_fetch(self):
        config = {
            "ai": {
                "enabled": True,
                "provider": "openai",
                "news_gate": {"enabled": True},
                "trade_idea_critic": {"enabled": True, "veto_on": "major_concern"},
            }
        }
        called = []

        def fake_fetcher(*_a, **_kw):
            called.append("news")
            return ["AAPL lawsuit"]

        result = evaluate_external_ai(
            _csp_order(),
            config=config,
            structural_severity="major",
            fetcher=fake_fetcher,
            llm=lambda *_a, **_kw: {"severity": "major", "concerns": ["x"]},
            env={"NEWS_API_KEY": "news-key", "OPENAI_API_KEY": "sk-test"},
        )
        self.assertEqual(result["veto_reason"], "")
        self.assertEqual(called, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
