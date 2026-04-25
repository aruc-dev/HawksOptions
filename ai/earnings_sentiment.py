"""Placeholder sizing helper for optional earnings sentiment analysis."""

from __future__ import annotations


def suggested_risk_fraction(sentiment_score: float, iv_crush_score: float) -> float:
    sentiment_score = max(-1.0, min(1.0, float(sentiment_score)))
    iv_crush_score = max(0.0, min(1.0, float(iv_crush_score)))
    base = 0.33 * (1.0 + max(0.0, sentiment_score) * 0.25)
    return round(base * iv_crush_score, 4)
