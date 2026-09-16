import logging
from collections.abc import Sequence
from dataclasses import dataclass

from qmemo_radar.application.filtering import (
    MANUAL_SOURCE_KEY,
    FilterPolicy,
    first_filter_reason,
)
from qmemo_radar.application.normalization import build_candidate
from qmemo_radar.application.ports import EventRepository, Ranker, SourceCollector
from qmemo_radar.application.scoring import calculate_total
from qmemo_radar.domain import EventCandidate, EventStatus, PipelineCounters, ScoreResult
from qmemo_radar.exceptions import RankingFailed

logger = logging.getLogger(__name__)


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

    async def run_once(self, *, run_id: str | None = None) -> PipelineCounters:
        counters = PipelineCounters()
        checkpoints = await self._repository.get_checkpoints()
        for fetch in await self._collector.collect(checkpoints):
            log = {"run_id": run_id, "operation": "collect", "source_key": fetch.source_key}
            if fetch.error_code:
                counters.source_errors += 1
                await self._repository.record_source_result(
                    fetch.source_key, cursor=None, error_code=fetch.error_code
                )
                logger.warning(
                    "source failed",
                    extra={**log, "result": "failed", "error_code": fetch.error_code},
                )
                continue

            counters.collected += len(fetch.items)
            for raw_item in fetch.items:
                inserted = await self._repository.add_event(build_candidate(raw_item))
                if not inserted:
                    counters.duplicates += 1
                    continue
                counters.inserted += 1
            # A storage error above aborts the run: the cursor moves only after every item is saved.
            await self._repository.record_source_result(
                fetch.source_key, cursor=fetch.cursor, error_code=None
            )
            logger.info("source collected", extra={**log, "result": f"items={len(fetch.items)}"})

        pending = await self._repository.list_events_by_status(
            EventStatus.DISCOVERED,
            limit=100,
        )
        candidates: list[EventCandidate] = []
        for event in pending:
            reason = first_filter_reason(event, self._filter_policy)
            if reason is None and await self._repository.has_earlier_content_duplicate(event):
                reason = "duplicate_content"
            if reason:
                await self._repository.set_status(
                    event.event_id,
                    EventStatus.FILTERED_OUT,
                    expected={EventStatus.DISCOVERED},
                    filter_reason=reason,
                )
                logger.info(
                    "event filtered",
                    extra={
                        "run_id": run_id,
                        "event_id": event.event_id,
                        "operation": "filter",
                        "result": reason,
                    },
                )
                counters.filtered += 1
                continue
            candidates.append(event)

        for batch in _batches(candidates, size=10):
            if not await self._rank(batch, counters, run_id):
                break

        return counters

    async def _rank(
        self,
        batch: list[EventCandidate],
        counters: PipelineCounters,
        run_id: str | None,
    ) -> bool:
        """Score one batch. Returns False when the provider is down and ranking should stop."""
        try:
            by_id = _validate_results(batch, await self._ranker.rank(batch))
        except RankingFailed as exc:
            logger.warning(
                "ranking batch failed",
                extra={
                    "run_id": run_id,
                    "operation": "rank",
                    "result": f"failed_batch={len(batch)}",
                    "error_code": exc.code,
                },
            )
            if exc.retryable:
                # Provider unreachable: events stay DISCOVERED and are retried next run.
                counters.rank_failed += len(batch)
                return False
            if len(batch) > 1:
                # One bad post must not block its neighbours: rank them one by one.
                for event in batch:
                    if not await self._rank([event], counters, run_id):
                        return False
                return True
            # This single post keeps producing invalid answers: never show it, never retry it.
            counters.rank_failed += 1
            await self._repository.set_status(
                batch[0].event_id,
                EventStatus.FILTERED_OUT,
                expected={EventStatus.DISCOVERED},
                filter_reason="rank_invalid_output",
            )
            return True

        for event in batch:
            score = by_id[event.event_id]
            next_status = (
                EventStatus.SHORTLISTED  # a manual link was chosen by a person
                if event.source_key == MANUAL_SOURCE_KEY
                else self._status_for_score(score.total)
            )
            if not await self._repository.save_score_and_status(score, next_status):
                continue  # expired or otherwise moved on while it was being ranked
            logger.info(
                "event scored",
                extra={
                    "run_id": run_id,
                    "event_id": event.event_id,
                    "operation": "rank",
                    "result": f"{next_status.value}:{score.total}",
                },
            )
            counters.scored += 1
            if next_status is EventStatus.SHORTLISTED:
                counters.shortlisted += 1
            else:
                counters.archived += 1
        return True

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
        raise RankingFailed("duplicate_event_ids")
    if set(result_ids) != expected_ids:
        raise RankingFailed("unexpected_event_ids")

    validated: dict[str, ScoreResult] = {}
    for result in results:
        calculated = calculate_total(result.breakdown)
        validated[result.event_id] = result.model_copy(update={"total": calculated})
    return validated
