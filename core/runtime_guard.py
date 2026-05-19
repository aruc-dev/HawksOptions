"""Runtime safety gates for scheduler entrypoints."""

from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Any


def assert_runtime_allowed(config: dict[str, Any], *, halt_file: Path) -> None:
    """Raise when the local runtime must not continue.

    The guard is intentionally small and deterministic: a halt file blocks every
    scheduler entrypoint, and live mode requires a date-scoped operator ack.
    """
    if halt_file.exists():
        reason = halt_file.read_text(encoding="utf-8").strip() or "halt file present"
        raise RuntimeError(f"hawksoptions_halted:{reason}")
    if str(config.get("mode", "paper")).lower() == "live":
        expected = date.today().isoformat()
        actual = os.getenv("HAWKSOPTIONS_LIVE_ACK", "").strip()
        if actual != expected:
            raise RuntimeError(f"live_mode_requires_HAWKSOPTIONS_LIVE_ACK={expected}")


def write_halt_file(path: Path, *, reason: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(reason.strip() or "manual halt", encoding="utf-8")
    path.chmod(0o600)
    return path


def clear_halt_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
