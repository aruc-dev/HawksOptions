"""Unit tests for core.config path resolution."""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from core.config import CONFIG_PATH, resolve_config_path


class TestResolveConfigPath(unittest.TestCase):
    """Tests for resolve_config_path()."""

    def test_explicit_path_is_returned_unchanged(self):
        """An explicit path argument is always returned, env var has no effect."""
        custom = Path("/tmp/my_custom_config.yaml")
        with patch.dict(os.environ, {"HAWKS_USE_LOCAL_CONFIG": "1"}):
            result = resolve_config_path(custom)
        self.assertEqual(result, custom)

    def test_explicit_path_returned_even_without_env_var(self):
        explicit = Path("/tmp/another.yaml")
        env = {k: v for k, v in os.environ.items() if k != "HAWKS_USE_LOCAL_CONFIG"}
        with patch.dict(os.environ, env, clear=True):
            result = resolve_config_path(explicit)
        self.assertEqual(result, explicit)

    def test_default_without_env_var_returns_committed_config(self):
        """Without the env var, load_config() always uses the committed config.yaml."""
        env = {k: v for k, v in os.environ.items() if k != "HAWKS_USE_LOCAL_CONFIG"}
        with patch.dict(os.environ, env, clear=True):
            # Simulate local config existing on disk — should still be ignored.
            with patch("core.config.LOCAL_CONFIG_PATH") as mock_local:
                mock_local.exists.return_value = True
                result = resolve_config_path()
        self.assertEqual(result, CONFIG_PATH)

    def test_local_config_used_when_env_var_set_and_file_exists(self):
        """LOCAL_CONFIG_PATH is selected when env var is set and the file exists."""
        with patch.dict(os.environ, {"HAWKS_USE_LOCAL_CONFIG": "1"}):
            with patch("core.config.LOCAL_CONFIG_PATH") as mock_local:
                mock_local.exists.return_value = True
                # resolve_config_path compares mock_local with LOCAL_CONFIG_PATH
                # so we need the function to return it correctly.
                import core.config as cfg_mod
                original = cfg_mod.LOCAL_CONFIG_PATH
                cfg_mod.LOCAL_CONFIG_PATH = mock_local
                try:
                    result = resolve_config_path()
                finally:
                    cfg_mod.LOCAL_CONFIG_PATH = original
        self.assertIs(result, mock_local)

    def test_local_config_absent_falls_back_to_committed_config(self):
        """When env var is set but local file is missing, fall back to config.yaml."""
        with patch.dict(os.environ, {"HAWKS_USE_LOCAL_CONFIG": "1"}):
            with patch("core.config.LOCAL_CONFIG_PATH") as mock_local:
                mock_local.exists.return_value = False
                import core.config as cfg_mod
                original = cfg_mod.LOCAL_CONFIG_PATH
                cfg_mod.LOCAL_CONFIG_PATH = mock_local
                try:
                    result = resolve_config_path()
                finally:
                    cfg_mod.LOCAL_CONFIG_PATH = original
        self.assertEqual(result, CONFIG_PATH)

    def test_env_var_empty_string_does_not_select_local_config(self):
        """An empty HAWKS_USE_LOCAL_CONFIG value is treated as not set."""
        with patch.dict(os.environ, {"HAWKS_USE_LOCAL_CONFIG": ""}):
            with patch("core.config.LOCAL_CONFIG_PATH") as mock_local:
                mock_local.exists.return_value = True
                import core.config as cfg_mod
                original = cfg_mod.LOCAL_CONFIG_PATH
                cfg_mod.LOCAL_CONFIG_PATH = mock_local
                try:
                    result = resolve_config_path()
                finally:
                    cfg_mod.LOCAL_CONFIG_PATH = original
        self.assertEqual(result, CONFIG_PATH)


if __name__ == "__main__":
    unittest.main()
