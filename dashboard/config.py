"""Dashboard configuration loader."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from core.config import load_config, load_yaml, resolve_config_path

BASE_DIR = Path(__file__).resolve().parent.parent

AUTH_MODE_LOCAL = "local"
AUTH_MODE_CLOUDFLARE = "cloudflare"
VALID_AUTH_MODES = {AUTH_MODE_LOCAL, AUTH_MODE_CLOUDFLARE}


class DashboardConfig:
    def __init__(self, config_path: Path | None = None) -> None:
        self._explicit_config_path = config_path is not None
        self.config_path = resolve_config_path(config_path)
        self._cached: dict[str, Any] | None = None

    def _load(self) -> dict[str, Any]:
        if self._cached is None:
            self._cached = load_yaml(self.config_path) if self._explicit_config_path else load_config()
        return self._cached

    @property
    def mode(self) -> str:
        return str(self._load().get("mode", "paper")).lower()

    @property
    def daily_loss_limit_pct(self) -> float:
        return float(self._load().get("account", {}).get("daily_loss_halt_pct", 0.05))

    @property
    def account_config(self) -> dict[str, Any]:
        section = self._load().get("account", {})
        return section if isinstance(section, dict) else {}

    @property
    def trade_log_path(self) -> Path:
        return BASE_DIR / str(self._load().get("reporting", {}).get("trade_log_file", "data/trades.csv"))

    @property
    def positions_path(self) -> Path:
        return BASE_DIR / str(self._load().get("reporting", {}).get("positions_file", "data/positions.json"))

    @property
    def iv_history_path(self) -> Path:
        return BASE_DIR / str(self._load().get("reporting", {}).get("iv_history_file", "data/iv_rank_history.csv"))

    @property
    def daily_baseline_path(self) -> Path:
        return BASE_DIR / "data" / "daily_loss_baseline.json"

    @property
    def logs_dir(self) -> Path:
        return BASE_DIR / str(self._load().get("reporting", {}).get("logs_dir", "logs"))

    @property
    def reports_dir(self) -> Path:
        return BASE_DIR / str(self._load().get("reporting", {}).get("reports_dir", "reports"))

    @property
    def health_snapshot_dir(self) -> Path:
        return self.reports_dir / "health_snapshots"

    @property
    def check_systemd_script(self) -> Path:
        return BASE_DIR / "scripts" / "check_systemd.sh"

    @property
    def underlyings_path(self) -> Path:
        source = str(self._load().get("underlyings", {}).get("source", "config/underlyings.yaml"))
        return BASE_DIR / source

    @property
    def ai_enabled(self) -> bool:
        return bool(self._load().get("ai", {}).get("enabled", False))


def get_auth_mode() -> str:
    mode = os.environ.get("DASHBOARD_AUTH_MODE", AUTH_MODE_CLOUDFLARE).lower()
    if mode not in VALID_AUTH_MODES:
        raise ValueError(f"Invalid DASHBOARD_AUTH_MODE={mode!r}; must be one of {VALID_AUTH_MODES}")
    return mode


def get_cf_team_domain() -> str:
    return os.environ.get("CF_ACCESS_TEAM_DOMAIN", "")


def get_cf_audience() -> str:
    return os.environ.get("CF_ACCESS_AUD", "")


def get_allowed_emails() -> set[str]:
    raw = os.environ.get("DASHBOARD_ALLOWED_EMAILS", "")
    return {item.strip().lower() for item in raw.split(",") if item.strip()}


_shared = DashboardConfig()


def cfg() -> DashboardConfig:
    return _shared
