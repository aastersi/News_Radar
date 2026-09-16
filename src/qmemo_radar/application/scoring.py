from qmemo_radar.domain import ScoreBreakdown


def calculate_total(breakdown: ScoreBreakdown) -> int:
    raw = (
        breakdown.qmemo_relevance
        + breakdown.quote_strength
        + breakdown.discussion_potential
        + breakdown.freshness
        + breakdown.clarity
        + breakdown.action_likelihood
        - breakdown.risk_penalty
    )
    return max(0, min(100, raw))

