"""Configuration helpers for HawksOptions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import yaml

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = BASE_DIR / "config" / "config.yaml"
LOCAL_CONFIG_PATH = BASE_DIR / "config" / "config.local.yaml"


def _resolve_config_path(path: Path | None) -> Path:
    if path is not None:
        return path
    return LOCAL_CONFIG_PATH if LOCAL_CONFIG_PATH.exists() else CONFIG_PATH


def load_yaml(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        payload = yaml.safe_load(handle) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"{path} must contain a top-level mapping")
    return payload


def load_config(path: Path | None = None) -> dict[str, Any]:
    config = load_yaml(_resolve_config_path(path))
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
