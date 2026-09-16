from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Protocol

from qmemo_radar.domain import (
    DeliveryKind,
    Draft,
    DraftText,
    EventCandidate,
    EventStatus,
    FeedbackAction,
    OutboxStatus,
    PipelineCounters,
    PipelineRun,
    PublicationPackage,
    RawSourceItem,
    RunStatus,
    ScoredEvent,
    ScoreResult,
    SourceFetch,
    SourceHealth,
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
    ) -> bool:
        """Store the score and status if the event is still DISCOVERED."""
        ...

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

    async def requeue_manual(self, event: EventCandidate) -> bool:
        """Mark an already stored but ARCHIVED or FILTERED_OUT post as a manual pick."""
        ...

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


class DraftWriter(Protocol):
    async def write(
        self,
        card: ScoredEvent,
        *,
        previous: Draft | None = None,
        instruction: str | None = None,
    ) -> DraftText:
        """Write a draft, or a revision of `previous`; raise DraftFailed when impossible."""
        ...


class DraftRepository(ReviewRepository, Protocol):
    async def get_draft(self, draft_id: str) -> Draft | None: ...

    async def latest_draft(self, event_id: str) -> Draft | None: ...

    async def latest_revisable_draft(self) -> Draft | None: ...

    async def save_first_draft(self, draft: Draft, *, telegram_user_id: int) -> bool:
        """Insert version 1 and move the event to DRAFTED atomically."""
        ...

    async def save_revision(
        self,
        draft: Draft,
        *,
        previous_id: str,
        action: FeedbackAction,
        telegram_user_id: int,
    ) -> bool:
        """Insert version 2 and supersede the previous version atomically."""
        ...

    async def mark_verified(self, draft_id: str, *, telegram_user_id: int) -> bool: ...

    async def reject_draft(self, draft: Draft, *, telegram_user_id: int) -> bool: ...


class OutboxRepository(DraftRepository, Protocol):
    async def approve(
        self,
        draft_id: str,
        *,
        telegram_user_id: int,
        build_package: Callable[[EventCandidate, Draft], PublicationPackage],
    ) -> PublicationPackage | None:
        """In one transaction: re-read the event and latest draft, build the package,
        insert it with ON CONFLICT DO NOTHING, store feedback and mark the event APPROVED.
        Returns None and changes nothing when the state no longer allows approval."""
        ...

    async def get_package(self, event_id: str) -> PublicationPackage | None: ...

    async def list_packages(
        self, status: OutboxStatus, *, limit: int
    ) -> list[PublicationPackage]: ...

    async def count_packages(self, status: OutboxStatus) -> int: ...


class RunRepository(OutboxRepository, Protocol):
    async def start_run(self, run_id: str) -> None: ...

    async def finish_run(
        self,
        run_id: str,
        status: RunStatus,
        counters: PipelineCounters,
        error_code: str | None,
    ) -> None: ...

    async def fail_interrupted_runs(self) -> int: ...

    async def last_run(self, statuses: set[RunStatus] | None = None) -> PipelineRun | None: ...

    async def counters_since(self, since: datetime) -> PipelineCounters: ...

    async def source_health(self) -> list[SourceHealth]: ...
