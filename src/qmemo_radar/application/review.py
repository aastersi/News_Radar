import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

from qmemo_radar.application.filtering import MANUAL_SOURCE_KEY
from qmemo_radar.application.normalization import build_candidate, parse_x_status_url
from qmemo_radar.application.ports import PostLookup, ReviewGateway, ReviewRepository
from qmemo_radar.domain import DeliveryKind, EventStatus, FeedbackAction, ScoredEvent
from qmemo_radar.exceptions import DeliveryFailed, SourceUnavailable

logger = logging.getLogger(__name__)

PAUSED_KEY = "paused"


class Outcome(StrEnum):
    DONE = "DONE"
    NOT_FOUND = "NOT_FOUND"
    ALREADY_DECIDED = "ALREADY_DECIDED"
    INVALID = "INVALID"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class DeliveryLimits:
    daily_cards: int = 10
    digest_cards: int = 5
    urgent_threshold: int = 80
    event_ttl: timedelta = timedelta(hours=48)


@dataclass(frozen=True, slots=True)
class TodayReport:
    delivered: list[ScoredEvent]
    waiting: int
    remaining: int


class ReviewService:
    """Card delivery and card decisions. Knows nothing about Telegram itself."""

    def __init__(
        self,
        *,
        repository: ReviewRepository,
        gateway: ReviewGateway,
        chat_id: int,
        limits: DeliveryLimits,
        timezone: ZoneInfo,
        lookup: PostLookup | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._gateway = gateway
        self._chat_id = chat_id
        self._limits = limits
        self._timezone = timezone
        self._lookup = lookup
        self._clock = clock
        # ponytail: in-process lock, enough for the single Radar container.
        self._delivery_lock = asyncio.Lock()

    async def deliver(self, *, urgent: bool) -> int:
        """Send the best waiting cards within the daily limit. Returns how many were sent."""
        async with self._delivery_lock:
            if await self.is_paused():
                return 0
            remaining = await self._remaining_today()
            if urgent:
                statuses = {EventStatus.SHORTLISTED}
                min_total, limit = self._limits.urgent_threshold, remaining
            else:
                statuses = {EventStatus.SHORTLISTED, EventStatus.SNOOZED}
                min_total, limit = 0, min(self._limits.digest_cards, remaining)
            if limit <= 0:
                return 0

            kind = DeliveryKind.URGENT if urgent else DeliveryKind.DIGEST
            sent = 0
            for card in await self._repository.list_deliverable(
                statuses, min_total=min_total, limit=limit
            ):
                log = {"event_id": card.event.event_id, "operation": f"deliver_{kind.value}"}
                try:
                    message_id = await self._gateway.send_card(card, urgent=urgent)
                except DeliveryFailed as exc:
                    # Nothing was recorded, so the same card is retried on the next delivery.
                    logger.warning(
                        "card delivery failed",
                        extra={**log, "result": "failed", "error_code": exc.code},
                    )
                    continue
                if await self._repository.record_delivery(
                    card.event.event_id,
                    chat_id=self._chat_id,
                    message_id=message_id,
                    kind=kind,
                    expected=statuses,
                ):
                    sent += 1
                    logger.info("card delivered", extra={**log, "result": "notified"})
            return sent

    async def skip(self, event_id: str, user_id: int) -> Outcome:
        return await self._decide(
            event_id,
            EventStatus.SKIPPED,
            {EventStatus.NOTIFIED, EventStatus.SNOOZED},
            FeedbackAction.SKIP,
            user_id,
        )

    async def later(self, event_id: str, user_id: int) -> Outcome:
        return await self._decide(
            event_id, EventStatus.SNOOZED, {EventStatus.NOTIFIED}, FeedbackAction.LATER, user_id
        )

    async def explain(self, event_id: str) -> ScoredEvent | None:
        return await self._repository.get_scored_event(event_id)

    async def today(self) -> TodayReport:
        counts = await self._repository.count_by_status()
        return TodayReport(
            delivered=await self._repository.list_delivered_since(self._day_start()),
            waiting=counts.get(EventStatus.SHORTLISTED, 0) + counts.get(EventStatus.SNOOZED, 0),
            remaining=await self._remaining_today(),
        )

    async def snoozed(self, *, limit: int = 10) -> list[ScoredEvent]:
        return await self._repository.list_scored_by_status(EventStatus.SNOOZED, limit=limit)

    async def submit_link(self, url: str) -> Outcome:
        post_id = parse_x_status_url(url)
        if post_id is None:
            return Outcome.INVALID
        if self._lookup is None:
            return Outcome.UNAVAILABLE
        try:
            item = await self._lookup.lookup_post(post_id)
        except SourceUnavailable as exc:
            logger.warning(
                "manual link lookup failed",
                extra={"operation": "manual_link", "result": "failed", "error_code": exc.code},
            )
            return Outcome.UNAVAILABLE
        event = build_candidate(item.model_copy(update={"source_key": MANUAL_SOURCE_KEY}))
        if not await self._repository.add_event(event):
            # Known post: a person's pick overrides an earlier archive or filter decision.
            if not await self._repository.requeue_manual(event):
                return Outcome.ALREADY_DECIDED
        logger.info(
            "manual link queued",
            extra={"event_id": event.event_id, "operation": "manual_link", "result": "queued"},
        )
        return Outcome.DONE

    async def expire(self) -> int:
        return await self._repository.expire_events(self._clock() - self._limits.event_ttl)

    async def is_paused(self) -> bool:
        return await self._repository.get_state(PAUSED_KEY) == "1"

    async def set_paused(self, paused: bool) -> None:
        await self._repository.set_state(PAUSED_KEY, "1" if paused else "0")

    async def _decide(
        self,
        event_id: str,
        status: EventStatus,
        expected: set[EventStatus],
        action: FeedbackAction,
        user_id: int,
    ) -> Outcome:
        if await self._repository.get_scored_event(event_id) is None:
            return Outcome.NOT_FOUND
        changed = await self._repository.decide(
            event_id, status, expected=expected, action=action, telegram_user_id=user_id
        )
        return Outcome.DONE if changed else Outcome.ALREADY_DECIDED

    async def _remaining_today(self) -> int:
        sent = await self._repository.count_deliveries_since(self._day_start())
        return max(0, self._limits.daily_cards - sent)

    def _day_start(self) -> datetime:
        local = self._clock().astimezone(self._timezone)
        return local.replace(hour=0, minute=0, second=0, microsecond=0)
