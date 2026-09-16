from qmemo_radar.application.scoring import calculate_total
from qmemo_radar.domain import ScoreBreakdown


def test_score_is_recomputed_and_clamped() -> None:
    maximum = ScoreBreakdown(
        qmemo_relevance=30,
        quote_strength=20,
        discussion_potential=15,
        freshness=15,
        clarity=10,
        action_likelihood=10,
        risk_penalty=0,
    )
    risky = maximum.model_copy(update={"risk_penalty": 30, "qmemo_relevance": 0})

    assert calculate_total(maximum) == 100
    assert calculate_total(risky) == 40

