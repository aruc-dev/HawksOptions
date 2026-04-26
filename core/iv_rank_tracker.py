"""IV-rank history and calculations."""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from core.file_lock import atomic_write_text, lock_path_for, locked_open


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
    with locked_open(lock_path_for(path), "a", lock="exclusive"):
        # Check the on-disk size *after* acquiring the lock; tell()
        # on a process-local stream can return 0 even when another
        # process has just written content under append-mode races.
        try:
            file_size = path.stat().st_size
        except FileNotFoundError:
            file_size = 0
        with open(path, "a", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            if file_size == 0:
                writer.writerow(["timestamp", "symbol", "implied_volatility"])
            writer.writerow([timestamp.isoformat(timespec="seconds"), symbol, f"{float(implied_volatility):.6f}"])


def load_iv_history(path: Path, symbol: str, lookback_days: int = 365) -> list[float]:
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(lookback_days)))
    out: list[float] = []
    with locked_open(lock_path_for(path), "a", lock="shared"):
        if not path.exists():
            return []
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


def prune_iv_history(path: Path, max_age_days: int) -> int:
    """Drop IV snapshots older than ``max_age_days`` from the history file.

    Used to bound the size of ``iv_rank_history.csv`` over time. Returns
    the number of rows dropped. The file is rewritten atomically.
    """
    if max_age_days <= 0:
        return 0
    cutoff = datetime.now(timezone.utc) - timedelta(days=int(max_age_days))
    kept_rows: list[dict[str, str]] = []
    dropped = 0
    with locked_open(lock_path_for(path), "a", lock="exclusive"):
        if not path.exists():
            return 0
        with open(path, "r", newline="", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            fieldnames = list(reader.fieldnames or ["timestamp", "symbol", "implied_volatility"])
            for row in reader:
                ts_text = str(row.get("timestamp", "")).strip()
                if ts_text.endswith("Z"):
                    ts_text = ts_text[:-1] + "+00:00"
                try:
                    timestamp = datetime.fromisoformat(ts_text)
                except (TypeError, ValueError):
                    # Malformed rows are dropped; they would be unreadable
                    # later anyway.
                    dropped += 1
                    continue
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                if timestamp >= cutoff:
                    kept_rows.append(row)
                else:
                    dropped += 1

        if dropped == 0:
            return 0

        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=fieldnames)
        writer.writeheader()
        for row in kept_rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
        atomic_write_text(path, buffer.getvalue(), lock=False)
        return dropped
