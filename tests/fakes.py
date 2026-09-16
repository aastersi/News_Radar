"""Test doubles shared by the review, drafting and end-to-end tests."""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import HttpUrl

from qmemo_radar.application.filtering import FilterPolicy
from qmemo_radar.application.pipeline import PipelineThresholds, RadarPipeline
from qmemo_radar.application.review import DeliveryLimits, ReviewService
from qmemo_radar.domain import Engagement, RawSourceItem, ScoredEvent, SourceType
from qmemo_radar.exceptions import DeliveryFailed, SourceUnavailable
from qmemo_radar.infrastructure.collectors import FakeCollector
from qmemo_radar.infrastructure.ranking import DeterministicFixtureRanker
from qmemo_radar.infrastructure.storage import SQLiteEventRepository
from qmemo_radar.interfaces.telegram.controller import Reply

OWNER_ID = 424242
TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")


class FakeGateway:
    """Stands in for Telegram. Records cards instead of sending them."""

    def __init__(self) -> None:
        self.cards: list[tuple[ScoredEvent, bool, int]] = []
        self.fail = False
        self._next_message_id = 100

    async def send_card(self, card: ScoredEvent, *, urgent: bool) -> int:
        if self.fail:
            raise DeliveryFailed("TelegramNetworkError")
        self._next_message_id += 1
        self.cards.append((card, urgent, self._next_message_id))
        return self._next_message_id


class FakeLookup:
    def __init__(self, item: RawSourceItem | None = None) -> None:
        self.item = item
        self.calls: list[str] = []

    async def lookup_post(self, post_id: str) -> RawSourceItem:
        self.calls.append(post_id)
        if self.item is None:
            raise SourceUnavailable("not_found")
        return self.item


class Inbox:
    def __init__(self) -> None:
        self.replies: list[Reply] = []

    async def __call__(self, reply: Reply) -> None:
        self.replies.append(reply)

    @property
    def last(self) -> Reply:
        return self.replies[-1]


def x_item(
    number: int,
    text: str | None = None,
    *,
    replies: int = 40,
    minutes_ago: int = 5,
) -> RawSourceItem:
    return RawSourceItem(
        source=SourceType.X,
        external_id=str(1000 + number),
        url=HttpUrl(f"https://x.com/founder/status/{1000 + number}"),
        author_id="u1",
        author_handle="founder",
        author_display_name="Founder Name",
        original_text=text
        or f'Founder said: "Prediction number {number} will be remembered next year."',
        language="en",
        published_at=datetime.now(UTC) - timedelta(minutes=minutes_ago),
        engagement=Engagement(replies=replies, likes=10),
    )


async def seed(repository: SQLiteEventRepository, *items: RawSourceItem) -> None:
    """Run the deterministic pipeline so the items become scored events."""
    await RadarPipeline(
        collector=FakeCollector(list(items)),
        ranker=DeterministicFixtureRanker(),
        repository=repository,
        filter_policy=FilterPolicy(max_age=timedelta(hours=1)),
        thresholds=PipelineThresholds(),
    ).run_once()


def review_service(
    repository: SQLiteEventRepository,
    gateway: FakeGateway,
    *,
    lookup: FakeLookup | None = None,
    limits: DeliveryLimits | None = None,
) -> ReviewService:
    return ReviewService(
        repository=repository,
        gateway=gateway,
        chat_id=OWNER_ID,
        limits=limits or DeliveryLimits(),
        timezone=TIMEZONE,
        lookup=lookup,
    )
