"""Confidence scoring rubric (BUSINESS_RULES.md section 4).

Blends the deterministic rule-engine outcome with an AI-produced
plausibility score into a single auditable posterior ConfidenceScore,
plus a row-level verdict (BUSINESS_RULES.md section 5).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from .rules_engine import (
    Finding,
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
)

PENALTY = {
    SEVERITY_CRITICAL: 0.35,
    SEVERITY_MAJOR: 0.15,
    SEVERITY_MINOR: 0.05,
    "INFO": 0.0,
}

SEVERITY_CAP = {
    SEVERITY_CRITICAL: 0.40,
    SEVERITY_MAJOR: 0.70,
    SEVERITY_MINOR: 1.0,
    "INFO": 1.0,
}

RULE_WEIGHT = 0.7
AI_WEIGHT = 0.3

VERDICT_AUTO_APPROVED = "AUTO_APPROVED"
VERDICT_NEEDS_REVIEW = "NEEDS_REVIEW"
VERDICT_REJECTED = "REJECTED"


def _clamp(value: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, value))


@dataclass
class ScoreResult:
    base_score: float
    ai_plausibility_score: Optional[float]
    final_score: float
    verdict: str
    findings: List[Finding] = field(default_factory=list)
    ai_rationale: Optional[str] = None


def compute_base_score(findings: List[Finding]) -> float:
    """Deterministic component of the score: 1.0 minus per-finding penalties,
    capped by the worst severity present (BR-30/BR-31)."""
    score = 1.0
    worst_cap = 1.0
    for f in findings:
        score -= PENALTY.get(f.severity, 0.0)
        worst_cap = min(worst_cap, SEVERITY_CAP.get(f.severity, 1.0))
    score = _clamp(score)
    return min(score, worst_cap)


def compute_verdict(final_score: float, findings: List[Finding]) -> str:
    critical_count = sum(1 for f in findings if f.severity == SEVERITY_CRITICAL)
    if critical_count >= 2:
        return VERDICT_REJECTED
    if critical_count == 1:
        return VERDICT_NEEDS_REVIEW if final_score >= 0.60 else VERDICT_REJECTED
    if final_score >= 0.85:
        return VERDICT_AUTO_APPROVED
    if final_score >= 0.60:
        return VERDICT_NEEDS_REVIEW
    return VERDICT_REJECTED


def score_row(
    findings: List[Finding],
    ai_plausibility_score: Optional[float] = None,
    ai_rationale: Optional[str] = None,
) -> ScoreResult:
    base_score = compute_base_score(findings)

    if ai_plausibility_score is None:
        # No AI adjustment available (e.g. offline/dry-run mode) -- fall back
        # to the deterministic score alone, per BR-33 (never silently drop
        # the audit trail, just note that the AI layer did not run).
        final_score = base_score
    else:
        final_score = _clamp(RULE_WEIGHT * base_score + AI_WEIGHT * ai_plausibility_score)
        # A CRITICAL/MAJOR cap still applies even after AI blending.
        worst_cap = min(
            (SEVERITY_CAP.get(f.severity, 1.0) for f in findings), default=1.0
        )
        final_score = min(final_score, worst_cap)

    verdict = compute_verdict(final_score, findings)

    return ScoreResult(
        base_score=base_score,
        ai_plausibility_score=ai_plausibility_score,
        final_score=round(final_score, 3),
        verdict=verdict,
        findings=findings,
        ai_rationale=ai_rationale,
    )
