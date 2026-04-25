from __future__ import annotations

import os
import unittest
from unittest.mock import patch


def _skip_if_fastapi_missing():
    try:
        import fastapi  # noqa: F401
        from fastapi.testclient import TestClient  # noqa: F401
    except ImportError:
        raise unittest.SkipTest("fastapi not installed")


class DashboardAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_if_fastapi_missing()
        cls.env_patcher = patch.dict(os.environ, {"DASHBOARD_AUTH_MODE": "local"}, clear=False)
        cls.env_patcher.start()
        from dashboard.app import create_app
        from fastapi.testclient import TestClient

        cls.client = TestClient(create_app())

    @classmethod
    def tearDownClass(cls):
        cls.env_patcher.stop()

    def test_healthz_returns_json(self):
        from dashboard import app as app_module

        with patch.object(app_module, "alpaca_reachable", return_value=False):
            response = self.client.get("/healthz")
        self.assertIn(response.status_code, (200, 503))
        self.assertIn("status", response.json())

    def test_state_endpoint_shape(self):
        from dashboard import app as app_module

        with patch.object(app_module, "get_account", return_value={"portfolio_value": 100000.0}), \
                patch.object(app_module, "get_account_summary", return_value={"portfolio_value": 100000.0, "cash": 50000.0, "buying_power": 200000.0}), \
                patch.object(app_module, "alpaca_reachable", return_value=True), \
                patch.object(app_module, "read_positions_snapshot", return_value=[{"strategy_id": "s1", "underlying": "SPY", "strategy_name": "vertical_spread", "days_to_expiration": 30, "entry_credit": 50.0, "current_pnl": 10.0, "current_close_cost": 40.0, "short_delta": -0.2}]), \
                patch.object(app_module, "build_open_strategy_rows", return_value=[{"strategy_id": "s1"}]), \
                patch.object(app_module, "build_iv_rank_heatmap", return_value=[{"symbol": "SPY", "iv_rank": 45.0}]), \
                patch.object(app_module, "build_earnings_calendar", return_value=[]), \
                patch.object(app_module, "read_trades", return_value=[]), \
                patch.object(app_module, "read_ai_activity", return_value={"enabled": False}), \
                patch.object(app_module, "_build_health", return_value={"status": "green", "systemd": {"error": None, "stdout_tail": []}, "log_issues": []}):
            response = self.client.get("/api/state")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("open_strategies", body)
        self.assertIn("portfolio_greeks", body)
        self.assertIn("iv_rank_heatmap", body)
        self.assertIn("upcoming_earnings", body)

    def test_no_mutation_endpoints_exist(self):
        from dashboard.app import create_app

        app = create_app()
        forbidden = ["order", "buy", "sell", "cancel", "close_position"]
        for route in app.routes:
            path = getattr(route, "path", "")
            for token in forbidden:
                if token in path.lower():
                    self.fail(f"suspicious route path: {path}")

    def test_index_contains_hawksoptions(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("HawksOptions", response.text)

    def test_cloudflare_mode_rejects_without_jwt(self):
        from dashboard.app import create_app
        from fastapi.testclient import TestClient

        env = {
            "DASHBOARD_AUTH_MODE": "cloudflare",
            "CF_ACCESS_TEAM_DOMAIN": "test.cloudflareaccess.com",
            "CF_ACCESS_AUD": "aud",
            "DASHBOARD_ALLOWED_EMAILS": "arun@example.com",
        }
        with patch.dict(os.environ, env, clear=False):
            client = TestClient(create_app())
            self.assertEqual(client.get("/api/state").status_code, 401)


if __name__ == "__main__":
    unittest.main()
