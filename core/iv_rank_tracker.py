"""IV-rank history and calculations."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


def compute_iv_rank(current_iv: float, trailing_ivs: Iterable[float]) -> float:
    values = [float(value) for value in trailing_ivs]
    if not values:
        return 0.0
    low = min(values)
    high = max(values)
    if high == low:
        return 100.0 if current_iv >= high else 0.0
    raw = 100.0 * (float(current_iv) - low) / (high - low)
    return round(max(0.0, min(raw, 100.0)), 2)


def append_iv_snapshot(
    path: Path,
    symbol: str,
    implied_volatility: float,
    timestamp: datetime | None = None,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = timestamp or datetime.now(timezone.utc)
    file_exists = path.exists()
    with open(path, "a", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        if not file_exists:
            writer.writerow(["timestamp", "symbol", "implied_volatility"])
        writer.writerow([timestamp.isoformat(timespec="seconds"), symbol, f"{float(implied_volatility):.6f}"])


def load_iv_history(path: Path, symbol: str, lookback_days: int = 365) -> list[float]:
    if not path.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days)))
    out: list[float] = []
    with open(path, "r", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            if str(row.get("symbol", "")).upper() != str(symbol).upper():
                continue
            ts_text = str(row.get("timestamp", "")).strip()
            if ts_text.endswith("Z"):
                ts_text = ts_text[:-1] + "+00:00"
            try:
                timestamp = datetime.fromisoformat(ts_text)
                iv = float(row.get("implied_volatility", 0.0))
            except (TypeError, ValueError):
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            if timestamp >= cutoff:
                out.append(iv)
    return out
