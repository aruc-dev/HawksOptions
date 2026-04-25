"""Rule-backed news gate used when AI is disabled or unavailable."""

from __future__ import annotations

from typing import Iterable


RISK_KEYWORDS = {
    "lawsuit",
    "fraud",
    "guidance cut",
    "bankruptcy",
    "fda",
    "acquisition rumor",
    "investigation",
    "ceo resigns",
}


def evaluate_news_gate(headlines: Iterable[str], veto_threshold: float = 0.7) -> dict[str, object]:
    score = 0
    matches = []
    for headline in headlines:
        lowered = str(headline).lower()
        for keyword in RISK_KEYWORDS:
            if keyword in lowered:
                score += 1
                matches.append(keyword)
    confidence = min(1.0, score / 3.0)
    return {
        "veto": confidence >= veto_threshold,
        "confidence": round(confidence, 2),
        "reason": ", ".join(sorted(set(matches))) if matches else "no material risk keyword match",
    }
