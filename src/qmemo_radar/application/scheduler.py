import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from qmemo_radar.application.review import ReviewService
from qmemo_radar.application.runner import RadarRunner

logger = logging.getLogger(__name__)

Sleep = Callable[[float], Awaitable[None]]
EXPIRE_EVERY = timedelta(hours=1)
HEARTBEAT_EVERY = timedelta(minutes=1)


def next_occurrence(after: datetime, times: Sequence[time], timezone: ZoneInfo) -> datetime:
    """The first configured local wall-clock time strictly after `after`, in UTC."""
    moment_after = after.astimezone(UTC)
    local_date = after.astimezone(timezone).date()
    # Compare in UTC: subtracting two datetimes with the same ZoneInfo ignores DST shifts.
    upcoming = (
        datetime.combine(local_date + timedelta(days=day), moment, tzinfo=timezone).astimezone(UTC)
        for day in (0, 1, 2)
        for moment in times
    )
    return min(at for at in upcoming if at > moment_after)


class RadarScheduler:
    """Collection, digests, expiry and heartbeat. The outbox worker is intentionally absent."""

    def __init__(
        self,
        *,
        runner: RadarRunner,
        review: ReviewService,
        collect_every: timedelta,
        digest_times: Sequence[time],
        timezone: ZoneInfo,
        sleep: Sleep = asyncio.sleep,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._runner = runner
        self._review = review
        self._collect_every = collect_every
        self._digest_times = list(digest_times)
        self._timezone = timezone
        self._sleep = sleep
        self._clock = clock

    async def run(self) -> None:
        logger.info("scheduler started; outbox worker disabled", extra={"operation": "scheduler"})
        await asyncio.gather(
            self._every("heartbeat", HEARTBEAT_EVERY, self._runner.heartbeat),
            self._every("collect", self._collect_every, self._collect),
            self._every("expire", EXPIRE_EVERY, self._expire),
            self._digests(),
        )

    async def _collect(self) -> None:
        await self._runner.run_cycle(manual=False)

    async def _expire(self) -> None:
        expired = await self._review.expire()
        if expired:
            logger.info("events expired", extra={"operation": "expire", "result": str(expired)})

    async def _digest(self) -> None:
        sent = await self._review.deliver(urgent=False)
        logger.info("digest finished", extra={"operation": "digest", "result": f"sent={sent}"})

    async def _every(
        self, name: str, interval: timedelta, job: Callable[[], Awaitable[None]]
    ) -> None:
        while True:
            await _safely(name, job)
            await self._sleep(interval.total_seconds())

    async def _digests(self) -> None:
        slot = self._clock()
        while True:
            # Always move past the previous slot, so an early wake-up cannot send it twice.
            slot = next_occurrence(max(self._clock(), slot), self._digest_times, self._timezone)
            await self._sleep(max(0.0, (slot - self._clock()).total_seconds()))
            await _safely("digest", self._digest)


async def _safely(name: str, job: Callable[[], Awaitable[None]]) -> None:
    try:
        await job()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception(
            "scheduled job failed",
            extra={"operation": name, "result": "error", "error_code": type(exc).__name__},
        )
