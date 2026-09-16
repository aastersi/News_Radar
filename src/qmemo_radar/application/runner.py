import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

from qmemo_radar.application.pipeline import RadarPipeline
from qmemo_radar.application.ports import RunRepository
from qmemo_radar.application.review import ReviewService
from qmemo_radar.domain import (
    OutboxStatus,
    PipelineCounters,
    PipelineRun,
    RunStatus,
    SourceHealth,
)

logger = logging.getLogger(__name__)

HEARTBEAT_KEY = "heartbeat_at"


@dataclass(frozen=True, slots=True)
class CycleResult:
    status: RunStatus | None
    counters: PipelineCounters | None = None
    urgent_sent: int = 0
    skipped: str | None = None


@dataclass(frozen=True, slots=True)
class StatusReport:
    paused: bool
    heartbeat_at: datetime | None
    last_run: PipelineRun | None
    last_success: PipelineRun | None
    sources: list[SourceHealth]
    today: PipelineCounters
    sent_today: int
    outbox_approved: int
    qmemo_publishing_enabled: bool
    x_publishing_enabled: bool


class RadarRunner:
    """One entry point for scheduled and manual (/run) cycles, guarded by a single lock."""

    def __init__(
        self,
        *,
        pipeline: RadarPipeline,
        repository: RunRepository,
        review: ReviewService,
        timezone: ZoneInfo,
        qmemo_publishing_enabled: bool = False,
        x_publishing_enabled: bool = False,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._pipeline = pipeline
        self._repository = repository
        self._review = review
        self._timezone = timezone
        self._publishing = (qmemo_publishing_enabled, x_publishing_enabled)
        self._clock = clock
        # ponytail: in-process lock; one Radar container owns one SQLite file.
        self._lock = asyncio.Lock()

    async def run_cycle(self, *, manual: bool) -> CycleResult:
        if self._lock.locked():
            return CycleResult(status=None, skipped="already_running")
        async with self._lock:
            if not manual and await self._review.is_paused():
                return CycleResult(status=None, skipped="paused")
            run_id = uuid4().hex
            log = {"run_id": run_id, "operation": "cycle"}
            await self._repository.start_run(run_id)
            counters, error_code = PipelineCounters(), None
            try:
                counters = await self._pipeline.run_once(run_id=run_id)
                partial = counters.source_errors or counters.rank_failed
                status = RunStatus.PARTIAL if partial else RunStatus.SUCCESS
            except Exception as exc:
                # The failure is recorded as FAILED so /status shows it; the scheduler keeps going.
                status, error_code = RunStatus.FAILED, type(exc).__name__
                logger.exception(
                    "cycle failed", extra={**log, "result": "FAILED", "error_code": error_code}
                )
            await self._repository.finish_run(run_id, status, counters, error_code)
            logger.info("cycle finished", extra={**log, "result": status.value})
            urgent = 0 if status is RunStatus.FAILED else await self._review.deliver(urgent=True)
            return CycleResult(status=status, counters=counters, urgent_sent=urgent)

    async def heartbeat(self) -> None:
        await self._repository.set_state(HEARTBEAT_KEY, self._clock().isoformat())

    async def status(self) -> StatusReport:
        day_start = (
            self._clock()
            .astimezone(self._timezone)
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        heartbeat = await self._repository.get_state(HEARTBEAT_KEY)
        return StatusReport(
            paused=await self._review.is_paused(),
            heartbeat_at=datetime.fromisoformat(heartbeat) if heartbeat else None,
            last_run=await self._repository.last_run(),
            last_success=await self._repository.last_run({RunStatus.SUCCESS}),
            sources=await self._repository.source_health(),
            today=await self._repository.counters_since(day_start),
            sent_today=await self._repository.count_deliveries_since(day_start),
            outbox_approved=await self._repository.count_packages(OutboxStatus.APPROVED),
            qmemo_publishing_enabled=self._publishing[0],
            x_publishing_enabled=self._publishing[1],
        )
