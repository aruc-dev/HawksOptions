"""Minimal textfile metrics emission for operations checks."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def write_metrics_textfile(path: Path, metrics: dict[str, float | int]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, value in sorted(metrics.items()):
        safe_name = _metric_name(name)
        lines.append(f"# HELP {safe_name} HawksOptions scheduler metric.")
        lines.append(f"# TYPE {safe_name} gauge")
        lines.append(f"{safe_name} {float(value)}")
    generated = "hawksoptions_metrics_generated_at"
    lines.append(f"# HELP {generated} Unix timestamp when this textfile was generated.")
    lines.append(f"# TYPE {generated} gauge")
    lines.append(f"{generated} {datetime.now(timezone.utc).timestamp():.0f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _metric_name(name: str) -> str:
    return "hawksoptions_" + "".join(ch if ch.isalnum() else "_" for ch in name.lower())


def risk_check_metrics(payload: dict[str, Any]) -> dict[str, float | int]:
    daily_loss = payload.get("daily_loss", {}) if isinstance(payload.get("daily_loss"), dict) else {}
    return {
        "risk_actions_count": len(payload.get("actions", [])) if isinstance(payload.get("actions"), list) else 0,
        "risk_elevated_count": len(payload.get("elevated_positions", [])) if isinstance(payload.get("elevated_positions"), list) else 0,
        "risk_daily_loss_pct": float(daily_loss.get("loss_pct", 0.0) or 0.0),
    }


def risk_watch_metrics(payload: dict[str, Any]) -> dict[str, float | int]:
    return {
        "risk_watch_elevated_count": int(payload.get("elevated_count", 0) or 0),
        "risk_watch_triggered_extra_check": 1 if payload.get("triggered_extra_risk_check") else 0,
    }
