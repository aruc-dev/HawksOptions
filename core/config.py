"""Configuration helpers for HawksOptions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
LOCAL_CONFIG_PATH = BASE_DIR / "config" / "config.local.yaml"


def resolve_config_path(path: Path | None = None) -> Path:
    """Return a complete config file path.

    Resolution order:
    1. *path* — if an explicit path is supplied it is returned as-is.
    2. ``CONFIG_PATH`` — the committed reference config.

    ``load_config()`` is preferred for runtime use because it deep-merges
    ``config.local.yaml`` over ``config.yaml`` instead of loading the local
    file as a full replacement. This helper intentionally does not return the
    local overlay by default because that file can be partial.
    """
    if path is not None:
        return path
    return CONFIG_PATH


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a top-level mapping")
    return payload


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Return a new dict with *override* recursively merged into *base*."""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_config(path: Path | None = None) -> dict[str, Any]:
    if path is not None:
        config = load_yaml(path)
    else:
        config = load_yaml(CONFIG_PATH)
        if LOCAL_CONFIG_PATH.is_file():
            config = _deep_merge(config, load_yaml(LOCAL_CONFIG_PATH))

    config.setdefault("mode", "paper")
    config.setdefault("account", {})
    config.setdefault("gates", {})
    config.setdefault("strategies", {})
    config.setdefault("ai", {})
    config.setdefault("schedule", {})
    config.setdefault("reporting", {})
    return config


def load_underlyings(config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    config = deepcopy(config or load_config())
    source = config.get("underlyings", {}).get("source", "config/underlyings.yaml")
    payload = load_yaml(BASE_DIR / str(source))
    items = payload.get("underlyings", [])
    if not isinstance(items, list):
        raise ValueError("config/underlyings.yaml must contain an 'underlyings' list")
    return [item for item in items if isinstance(item, dict)]


def strategy_config(config: dict[str, Any], strategy_name: str) -> dict[str, Any]:
    strategy = config.get("strategies", {}).get(strategy_name, {})
    if not isinstance(strategy, dict):
        raise ValueError(f"strategy config for {strategy_name!r} must be a mapping")
    return strategy


def reporting_path(config: dict[str, Any], key: str) -> Path:
    reporting = config.get("reporting", {})
    rel = str(reporting.get(key, "")).strip()
    if not rel:
        raise KeyError(f"reporting.{key} is not configured")
    return BASE_DIR / rel


def ensure_runtime_dirs(config: dict[str, Any]) -> None:
    for key in ("greeks_snapshot_dir", "reports_dir", "logs_dir"):
        reporting_path(config, key).mkdir(parents=True, exist_ok=True)
    reporting_path(config, "trade_log_file").parent.mkdir(parents=True, exist_ok=True)
    reporting_path(config, "positions_file").parent.mkdir(parents=True, exist_ok=True)
