from collections.abc import Sequence

from qmemo_radar.application.scoring import calculate_total
from qmemo_radar.domain import EventCandidate, ScoreBreakdown, ScoreResult


class DeterministicFixtureRanker:
    """Offline ranker for tests and dry-runs. Never used as a production judge."""

    async def rank(self, events: Sequence[EventCandidate]) -> list[ScoreResult]:
        results: list[ScoreResult] = []
        for event in events:
            text = event.normalized_text
            relevance = 27 if any(word in text for word in ("quote", "prediction", "said")) else 18
            quote_strength = 18 if '"' in event.original_text else 12
            discussion = min(15, 7 + event.engagement.replies // 10)
            freshness = 15
            clarity = 9
            action = 8
            risk = 3 if "rumor" in text else 0
            breakdown = ScoreBreakdown(
                qmemo_relevance=relevance,
                quote_strength=quote_strength,
                discussion_potential=discussion,
                freshness=freshness,
                clarity=clarity,
                action_likelihood=action,
                risk_penalty=risk,
            )
            results.append(
                ScoreResult(
                    event_id=event.event_id,
                    breakdown=breakdown,
                    total=calculate_total(breakdown),
                    rationale="Offline fixture score for architecture verification",
                    recommended_format="quote_post",
                    target_action="save_quote",
                )
            )
        return results

