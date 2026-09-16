from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Protocol

from qmemo_radar.domain import (
    DeliveryKind,
    EventCandidate,
    EventStatus,
    FeedbackAction,
    PublicationPackage,
    RawSourceItem,
    ScoredEvent,
    ScoreResult,
    SourceFetch,
)


class SourceCollector(Protocol):
    async def collect(self, checkpoints: Mapping[str, str]) -> list[SourceFetch]:
        """Fetch each configured source independently; a failed source returns error_code."""
        ...


class PostLookup(Protocol):
    async def lookup_post(self, post_id: str) -> RawSourceItem: ...


class Ranker(Protocol):
    async def rank(self, events: Sequence[EventCandidate]) -> list[ScoreResult]: ...


class EventRepository(Protocol):
    async def initialize(self) -> None: ...

    async def add_event(self, event: EventCandidate) -> bool: ...

    async def list_events_by_status(
        self,
        status: EventStatus,
        *,
        limit: int,
    ) -> list[EventCandidate]: ...

    async def set_status(
        self,
        event_id: str,
        status: EventStatus,
        *,
        expected: set[EventStatus] | None = None,
        filter_reason: str | None = None,
    ) -> bool: ...

    async def save_score_and_status(
        self,
        score: ScoreResult,
        status: EventStatus,
    ) -> None: ...

    async def count_by_status(self) -> dict[str, int]: ...

    async def get_checkpoints(self) -> dict[str, str]: ...

    async def record_source_result(
        self,
        source_key: str,
        *,
        cursor: str | None,
        error_code: str | None,
    ) -> None: ...

    async def has_earlier_content_duplicate(self, event: EventCandidate) -> bool: ...


class ReviewRepository(EventRepository, Protocol):
    async def list_deliverable(
        self,
        statuses: set[EventStatus],
        *,
        min_total: int,
        limit: int,
    ) -> list[ScoredEvent]: ...

    async def record_delivery(
        self,
        event_id: str,
        *,
        chat_id: int,
        message_id: int,
        kind: DeliveryKind,
        expected: set[EventStatus],
    ) -> bool:
        """Store the Telegram message id and mark the event NOTIFIED in one transaction."""
        ...

    async def count_deliveries_since(self, since: datetime) -> int: ...

    async def list_delivered_since(self, since: datetime) -> list[ScoredEvent]: ...

    async def list_scored_by_status(
        self, status: EventStatus, *, limit: int
    ) -> list[ScoredEvent]: ...

    async def get_scored_event(self, event_id: str) -> ScoredEvent | None: ...

    async def decide(
        self,
        event_id: str,
        status: EventStatus,
        *,
        expected: set[EventStatus],
        action: FeedbackAction,
        telegram_user_id: int,
    ) -> bool:
        """Change the event status and store feedback atomically; False if the state moved on."""
        ...

    async def expire_events(self, discovered_before: datetime) -> int: ...

    async def get_state(self, key: str) -> str | None: ...

    async def set_state(self, key: str, value: str) -> None: ...


class ReviewGateway(Protocol):
    async def send_card(self, card: ScoredEvent, *, urgent: bool) -> int:
        """Send a card and return the Telegram message id; raise DeliveryFailed on failure."""
        ...


class QuotePublisher(Protocol):
    async def publish(self, package: PublicationPackage) -> str: ...


class XPublisher(Protocol):
    async def publish(self, package: PublicationPackage, qmemo_url: str) -> str: ...
