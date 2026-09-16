from collections.abc import Sequence
from typing import Protocol

from qmemo_radar.domain import (
    EventCandidate,
    EventStatus,
    PublicationPackage,
    RawSourceItem,
    ScoreResult,
)


class SourceCollector(Protocol):
    async def collect(self) -> list[RawSourceItem]: ...


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


class ReviewGateway(Protocol):
    async def send_candidates(
        self,
        events: Sequence[EventCandidate],
        scores: Sequence[ScoreResult],
    ) -> None: ...


class QuotePublisher(Protocol):
    async def publish(self, package: PublicationPackage) -> str: ...


class XPublisher(Protocol):
    async def publish(self, package: PublicationPackage, qmemo_url: str) -> str: ...
