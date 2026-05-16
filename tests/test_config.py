"""Unit tests for core.config local overlay handling."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import core.config as cfg_mod


class TestResolveConfigPath(unittest.TestCase):
    def test_explicit_path_is_returned_unchanged(self):
        custom = Path("/tmp/my_custom_config.yaml")
        result = cfg_mod.resolve_config_path(custom)
        self.assertEqual(result, custom)

    def test_default_returns_committed_config_when_local_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "config" / "config.yaml"
            local = root / "config" / "config.local.yaml"
            base.parent.mkdir()
            base.write_text("mode: paper\n", encoding="utf-8")

            with patch.object(cfg_mod, "CONFIG_PATH", base), patch.object(cfg_mod, "LOCAL_CONFIG_PATH", local):
                result = cfg_mod.resolve_config_path()

        self.assertEqual(result, base)

    def test_default_returns_committed_config_when_local_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "config" / "config.yaml"
            local = root / "config" / "config.local.yaml"
            base.parent.mkdir()
            base.write_text("mode: paper\n", encoding="utf-8")
            local.write_text("mode: live\n", encoding="utf-8")

            with patch.object(cfg_mod, "CONFIG_PATH", base), patch.object(cfg_mod, "LOCAL_CONFIG_PATH", local):
                result = cfg_mod.resolve_config_path()

        self.assertEqual(result, base)


class TestLocalConfigOverlay(unittest.TestCase):
    def test_deep_merge_preserves_nested_base_keys(self):
        base = {"mode": "paper", "account": {"max_positions": 5, "daily_loss_halt_pct": 0.03}}
        override = {"account": {"daily_loss_halt_pct": 0.02}}

        result = cfg_mod._deep_merge(base, override)

        self.assertEqual(result["mode"], "paper")
        self.assertEqual(result["account"]["max_positions"], 5)
        self.assertEqual(result["account"]["daily_loss_halt_pct"], 0.02)
        self.assertEqual(base["account"]["daily_loss_halt_pct"], 0.03)

    def test_load_config_uses_base_when_local_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "config" / "config.yaml"
            local = root / "config" / "config.local.yaml"
            base.parent.mkdir()
            base.write_text("mode: paper\naccount:\n  max_positions: 5\n", encoding="utf-8")

            with patch.object(cfg_mod, "CONFIG_PATH", base), patch.object(cfg_mod, "LOCAL_CONFIG_PATH", local):
                config = cfg_mod.load_config()

        self.assertEqual(config["mode"], "paper")
        self.assertEqual(config["account"]["max_positions"], 5)

    def test_load_config_deep_merges_local_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "config" / "config.yaml"
            local = root / "config" / "config.local.yaml"
            base.parent.mkdir()
            base.write_text(
                "mode: paper\n"
                "account:\n"
                "  max_positions: 5\n"
                "  daily_loss_halt_pct: 0.03\n"
                "reporting:\n"
                "  trade_log_file: data/trades.csv\n",
                encoding="utf-8",
            )
            local.write_text(
                "mode: live\n"
                "account:\n"
                "  daily_loss_halt_pct: 0.02\n",
                encoding="utf-8",
            )

            with patch.object(cfg_mod, "CONFIG_PATH", base), patch.object(cfg_mod, "LOCAL_CONFIG_PATH", local):
                config = cfg_mod.load_config()

        self.assertEqual(config["mode"], "live")
        self.assertEqual(config["account"]["max_positions"], 5)
        self.assertEqual(config["account"]["daily_loss_halt_pct"], 0.02)
        self.assertEqual(config["reporting"]["trade_log_file"], "data/trades.csv")

    def test_empty_local_config_does_not_break_base_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "config" / "config.yaml"
            local = root / "config" / "config.local.yaml"
            base.parent.mkdir()
            base.write_text("mode: paper\n", encoding="utf-8")
            local.write_text("", encoding="utf-8")

            with patch.object(cfg_mod, "CONFIG_PATH", base), patch.object(cfg_mod, "LOCAL_CONFIG_PATH", local):
                config = cfg_mod.load_config()

        self.assertEqual(config["mode"], "paper")

    def test_explicit_path_bypasses_local_overlay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "config" / "config.yaml"
            local = root / "config" / "config.local.yaml"
            base.parent.mkdir()
            base.write_text("mode: paper\n", encoding="utf-8")
            local.write_text("mode: live\n", encoding="utf-8")

            with patch.object(cfg_mod, "CONFIG_PATH", base), patch.object(cfg_mod, "LOCAL_CONFIG_PATH", local):
                config = cfg_mod.load_config(base)

        self.assertEqual(config["mode"], "paper")

    def test_dashboard_config_uses_merged_local_overlay(self):
        from dashboard.config import DashboardConfig

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            base = root / "config" / "config.yaml"
            local = root / "config" / "config.local.yaml"
            base.parent.mkdir()
            base.write_text(
                "mode: paper\n"
                "account:\n"
                "  daily_loss_halt_pct: 0.03\n"
                "reporting:\n"
                "  trade_log_file: data/trades.csv\n",
                encoding="utf-8",
            )
            local.write_text(
                "mode: live\n"
                "account:\n"
                "  daily_loss_halt_pct: 0.02\n",
                encoding="utf-8",
            )

            with patch.object(cfg_mod, "CONFIG_PATH", base), patch.object(cfg_mod, "LOCAL_CONFIG_PATH", local):
                config = DashboardConfig()
                mode = config.mode
                daily_loss_limit = config.daily_loss_limit_pct
                trade_log_path = config.trade_log_path

        self.assertEqual(mode, "live")
        self.assertEqual(daily_loss_limit, 0.02)
        self.assertEqual(trade_log_path.name, "trades.csv")


if __name__ == "__main__":
    unittest.main()
