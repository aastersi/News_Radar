"""Test doubles shared by the review, drafting and end-to-end tests."""

import itertools
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from pydantic import HttpUrl

from qmemo_radar.application.budget import BudgetGuard, PaidFeature
from qmemo_radar.application.filtering import FilterPolicy
from qmemo_radar.application.pipeline import PipelineThresholds, RadarPipeline
from qmemo_radar.application.ports import CostLedger, DraftWriter, Ranker, SourceCollector
from qmemo_radar.application.review import DeliveryLimits, ReviewService
from qmemo_radar.bootstrap import Application, build_services
from qmemo_radar.config import RadarSettings, SourcesConfig
from qmemo_radar.domain import CostEntry, Engagement, RawSourceItem, ScoredEvent, SourceType
from qmemo_radar.exceptions import DeliveryFailed, SourceUnavailable
from qmemo_radar.infrastructure.collectors import FakeCollector
from qmemo_radar.infrastructure.drafting import DeterministicDraftWriter
from qmemo_radar.infrastructure.ranking import DeterministicFixtureRanker
from qmemo_radar.infrastructure.storage import SQLiteEventRepository
from qmemo_radar.interfaces.telegram.controller import Reply, TelegramController

OWNER_ID = 424242
TIMEZONE = ZoneInfo("Asia/Ho_Chi_Minh")
X_EPOCH_MS = 1288834974657


def snowflake(sequence: int = 0, *, minutes_ago: float = 0) -> str:
    """A post id whose embedded timestamp is `minutes_ago` minutes in the past."""
    created_ms = int((datetime.now(UTC) - timedelta(minutes=minutes_ago)).timestamp() * 1000)
    return str(((created_ms - X_EPOCH_MS) << 22) + sequence)


class MemoryLedger:
    """In-memory CostLedger for adapter tests; the SQLite ledger has its own tests."""

    def __init__(self) -> None:
        self.entries: dict[int, CostEntry] = {}

    async def reserve_cost(
        self, entry: CostEntry, *, since: datetime, limit_usd: Decimal
    ) -> tuple[int, Decimal] | None:
        total = await self.cost_since(since) + entry.estimated_cost_usd
        if total > limit_usd:
            return None
        entry_id = len(self.entries) + 1
        self.entries[entry_id] = entry
        return entry_id, total

    async def settle_cost(self, entry_id: int, *, units: int, cost_usd: Decimal) -> None:
        entry = self.entries[entry_id]
        if cost_usd <= entry.estimated_cost_usd:
            self.entries[entry_id] = entry.model_copy(
                update={"units": units, "estimated_cost_usd": cost_usd}
            )

    async def cost_since(self, since: datetime) -> Decimal:
        return sum(
            (e.estimated_cost_usd for e in self.entries.values() if e.created_at >= since),
            Decimal(0),
        )


def open_guard(ledger: CostLedger | None = None, *, limit_usd: str = "10") -> BudgetGuard:
    """Every paid feature allowed, as an owner who enabled them would configure it."""
    return BudgetGuard(
        ledger or MemoryLedger(),
        enabled=frozenset(PaidFeature),
        hard_limit_usd=Decimal(limit_usd),
        target_usd=Decimal(limit_usd),
    )


LLM_CALL_USD = Decimal("0.01")


class FakeGateway:
    """Stands in for Telegram. Records cards instead of sending them."""

    _message_ids = itertools.count(101)  # unique per chat, like real Telegram message ids

    def __init__(self) -> None:
        self.cards: list[tuple[ScoredEvent, bool, int]] = []
        self.fail = False
        self.rejected_external_ids: set[str] = set()

    async def send_card(self, card: ScoredEvent, *, urgent: bool) -> int:
        if card.event.external_id in self.rejected_external_ids:
            raise DeliveryFailed("TelegramBadRequest")
        if self.fail:
            raise DeliveryFailed("TelegramNetworkError")
        message_id = next(self._message_ids)
        self.cards.append((card, urgent, message_id))
        return message_id


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


def settings_for(repository: SQLiteEventRepository) -> RadarSettings:
    return RadarSettings(_env_file=None, db_path=repository._db_path, allowed_telegram_id=OWNER_ID)


def telegram(
    repository: SQLiteEventRepository,
    gateway: FakeGateway,
    *,
    writer: DraftWriter | None = None,
    lookup: FakeLookup | None = None,
    collector: SourceCollector | None = None,
    ranker: Ranker | None = None,
) -> TelegramController:
    """The real composition from bootstrap with fake X, LLM and Telegram at the edges."""
    services = build_services(
        Application(settings=settings_for(repository), repository=repository),
        collector=collector or FakeCollector([]),
        ranker=ranker or DeterministicFixtureRanker(),
        writer=writer or DeterministicDraftWriter(),
        gateway=gateway,
        lookup=lookup,
        sources=SourcesConfig(),
    )
    return TelegramController(
        allowed_user_id=OWNER_ID,
        review=services.review,
        drafts=services.drafts,
        runner=services.runner,
        timezone=TIMEZONE,
    )
