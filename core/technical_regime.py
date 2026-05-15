"""Technical-regime helpers for optional strategy gates."""

from __future__ import annotations

from typing import Any


def technical_regime_context(source: dict[str, Any]) -> dict[str, float | None]:
    return {
        "trend_20d": _optional_float(source.get("trend_20d")),
        "trend_50d": _optional_float(source.get("trend_50d")),
        "rsi_14": _optional_float(source.get("rsi_14")),
        "price_vs_sma_50": _optional_float(source.get("price_vs_sma_50")),
    }


def technical_regime_passes(metrics: dict[str, float | None], params: dict[str, Any]) -> bool:
    checks = {
        "min_trend_20d": ("trend_20d", "min"),
        "max_trend_20d": ("trend_20d", "max"),
        "min_trend_50d": ("trend_50d", "min"),
        "max_trend_50d": ("trend_50d", "max"),
        "min_rsi_14": ("rsi_14", "min"),
        "max_rsi_14": ("rsi_14", "max"),
        "min_price_vs_sma_50": ("price_vs_sma_50", "min"),
        "max_price_vs_sma_50": ("price_vs_sma_50", "max"),
    }
    for param, (metric_name, bound) in checks.items():
        if param not in params:
            continue
        metric = metrics.get(metric_name)
        if metric is None:
            return False
        threshold = float(params[param])
        if bound == "min" and metric < threshold:
            return False
        if bound == "max" and metric > threshold:
            return False
    return True


def _optional_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
