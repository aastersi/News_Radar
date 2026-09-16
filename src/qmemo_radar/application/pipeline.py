from collections.abc import Sequence
from dataclasses import dataclass

from qmemo_radar.application.filtering import FilterPolicy, first_filter_reason
from qmemo_radar.application.normalization import build_candidate
from qmemo_radar.application.ports import EventRepository, Ranker, SourceCollector
from qmemo_radar.application.scoring import calculate_total
from qmemo_radar.domain import EventCandidate, EventStatus, PipelineCounters, ScoreResult


@dataclass(frozen=True, slots=True)
class PipelineThresholds:
    archive: int = 50
    digest: int = 65
    urgent: int = 80


class RadarPipeline:
    """One deterministic collection cycle. It never publishes externally."""

    def __init__(
        self,
        *,
        collector: SourceCollector,
        ranker: Ranker,
        repository: EventRepository,
        filter_policy: FilterPolicy,
        thresholds: PipelineThresholds,
    ) -> None:
        self._collector = collector
        self._ranker = ranker
        self._repository = repository
        self._filter_policy = filter_policy
        self._thresholds = thresholds

    async def run_once(self) -> PipelineCounters:
        counters = PipelineCounters()
        raw_items = await self._collector.collect()
        counters.collected = len(raw_items)

        for raw_item in raw_items:
            event = build_candidate(raw_item)
            inserted = await self._repository.add_event(event)
            if not inserted:
                counters.duplicates += 1
                continue
            counters.inserted += 1

        pending = await self._repository.list_events_by_status(
            EventStatus.DISCOVERED,
            limit=100,
        )
        candidates: list[EventCandidate] = []
        for event in pending:
            reason = first_filter_reason(event, self._filter_policy)
            if reason:
                await self._repository.set_status(
                    event.event_id,
                    EventStatus.FILTERED_OUT,
                    expected={EventStatus.DISCOVERED},
                    filter_reason=reason,
                )
                counters.filtered += 1
                continue
            candidates.append(event)

        for batch in _batches(candidates, size=10):
            results = await self._ranker.rank(batch)
            by_id = _validate_results(batch, results)
            for event in batch:
                score = by_id[event.event_id]
                next_status = self._status_for_score(score.total)
                await self._repository.save_score_and_status(score, next_status)
                counters.scored += 1
                if next_status is EventStatus.SHORTLISTED:
                    counters.shortlisted += 1
                else:
                    counters.archived += 1

        return counters

    def _status_for_score(self, total: int) -> EventStatus:
        if total >= self._thresholds.digest:
            return EventStatus.SHORTLISTED
        return EventStatus.ARCHIVED


def _batches(events: Sequence[EventCandidate], *, size: int) -> list[list[EventCandidate]]:
    return [list(events[index : index + size]) for index in range(0, len(events), size)]


def _validate_results(
    events: Sequence[EventCandidate],
    results: Sequence[ScoreResult],
) -> dict[str, ScoreResult]:
    expected_ids = {event.event_id for event in events}
    result_ids = [result.event_id for result in results]
    if len(result_ids) != len(set(result_ids)):
        raise ValueError("Ranker returned duplicate event ids")
    if set(result_ids) != expected_ids:
        raise ValueError("Ranker result ids do not match the input batch")

    validated: dict[str, ScoreResult] = {}
    for result in results:
        calculated = calculate_total(result.breakdown)
        validated[result.event_id] = result.model_copy(update={"total": calculated})
    return validated
