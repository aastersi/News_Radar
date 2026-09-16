import asyncio
import sqlite3
from datetime import UTC, datetime
from typing import Any

import pytest
from fakes import OWNER_ID, FakeGateway, Inbox, review_service, seed, telegram, x_item
from pydantic import ValidationError

from qmemo_radar.application.drafting import DraftOutcome, DraftService, RevisionMode
from qmemo_radar.application.outbox import build_publication_package, idempotency_key
from qmemo_radar.domain import (
    QMEMO_URL_PLACEHOLDER,
    DraftText,
    EventStatus,
    FactCheckStatus,
    OutboxStatus,
    ScoredEvent,
)
from qmemo_radar.infrastructure.drafting import DeterministicDraftWriter
from qmemo_radar.infrastructure.storage import SQLiteEventRepository


class NeedsReviewWriter(DeterministicDraftWriter):
    async def write(self, card: ScoredEvent, **kwargs: Any) -> DraftText:
        draft = await super().write(card, **kwargs)
        return draft.model_copy(update={"fact_check_required": True})


async def drafted(repository: SQLiteEventRepository, service: DraftService) -> str:
    gateway = FakeGateway()
    await seed(repository, x_item(1))
    await review_service(repository, gateway).deliver(urgent=False)
    result = await service.use(gateway.cards[0][0].event.event_id, OWNER_ID)
    assert result.draft is not None
    return result.draft.draft_id


def table(repository: SQLiteEventRepository, query: str) -> list[tuple[Any, ...]]:
    with sqlite3.connect(repository._db_path) as db:
        return db.execute(query).fetchall()


async def test_approval_creates_exactly_one_outbox_package(
    repository: SQLiteEventRepository,
) -> None:
    service = DraftService(repository=repository, writer=DeterministicDraftWriter())
    draft_id = await drafted(repository, service)

    first = await service.accept(draft_id, OWNER_ID)
    second = await service.accept(draft_id, OWNER_ID)

    assert first.outcome is DraftOutcome.APPROVED and first.package is not None
    assert second.outcome is DraftOutcome.ALREADY_APPROVED
    assert second.package == first.package
    package = first.package
    assert package.fact_check_status is FactCheckStatus.VERIFIED
    assert QMEMO_URL_PLACEHOLDER in package.x_text_template
    assert str(package.source_url) == "https://x.com/founder/status/1001"
    assert package.source_external_id == "1001"
    assert package.quote_author == "Founder Name (@founder)"
    assert package.status is OutboxStatus.APPROVED
    assert table(repository, "SELECT COUNT(*), status FROM publication_outbox") == [(1, "APPROVED")]
    assert table(repository, "SELECT action FROM feedback WHERE action = 'ACCEPT'") == [("ACCEPT",)]
    assert table(repository, "SELECT status FROM drafts") == [("ACCEPTED",)]
    assert await repository.count_by_status() == {EventStatus.APPROVED.value: 1}


async def test_concurrent_approval_still_creates_one_package(
    repository: SQLiteEventRepository,
) -> None:
    service = DraftService(repository=repository, writer=DeterministicDraftWriter())
    draft_id = await drafted(repository, service)

    results = await asyncio.gather(*(service.accept(draft_id, OWNER_ID) for _ in range(3)))

    assert [r.outcome for r in results].count(DraftOutcome.APPROVED) == 1
    assert table(repository, "SELECT COUNT(*) FROM publication_outbox") == [(1,)]
    assert table(repository, "SELECT COUNT(*) FROM feedback WHERE action = 'ACCEPT'") == [(1,)]


async def test_unverified_facts_block_approval_until_manual_check(
    repository: SQLiteEventRepository,
) -> None:
    service = DraftService(repository=repository, writer=NeedsReviewWriter())
    draft_id = await drafted(repository, service)

    blocked = await service.accept(draft_id, OWNER_ID)
    assert blocked.outcome is DraftOutcome.NEEDS_VERIFICATION
    assert table(repository, "SELECT COUNT(*) FROM publication_outbox") == [(0,)]

    await service.verify(draft_id, OWNER_ID)
    assert (await service.accept(draft_id, OWNER_ID)).outcome is DraftOutcome.APPROVED


async def test_failure_inside_the_transaction_leaves_no_partial_state(
    repository: SQLiteEventRepository,
) -> None:
    service = DraftService(repository=repository, writer=DeterministicDraftWriter())
    draft_id = await drafted(repository, service)
    with sqlite3.connect(repository._db_path) as db:
        db.execute(
            """
            CREATE TRIGGER injected_failure BEFORE UPDATE OF status ON radar_events
            WHEN NEW.status = 'APPROVED'
            BEGIN SELECT RAISE(ABORT, 'injected failure'); END
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        await service.accept(draft_id, OWNER_ID)

    assert table(repository, "SELECT COUNT(*) FROM publication_outbox") == [(0,)]
    assert table(repository, "SELECT COUNT(*) FROM feedback WHERE action = 'ACCEPT'") == [(0,)]
    assert table(repository, "SELECT status FROM drafts") == [("ACTIVE",)]
    assert await repository.count_by_status() == {EventStatus.DRAFTED.value: 1}

    with sqlite3.connect(repository._db_path) as db:
        db.execute("DROP TRIGGER injected_failure")
    assert (await service.accept(draft_id, OWNER_ID)).outcome is DraftOutcome.APPROVED
    assert table(repository, "SELECT COUNT(*) FROM publication_outbox") == [(1,)]


async def test_superseded_version_cannot_be_approved(repository: SQLiteEventRepository) -> None:
    service = DraftService(repository=repository, writer=DeterministicDraftWriter())
    first_id = await drafted(repository, service)

    revised = await service.revise(first_id, OWNER_ID, RevisionMode.SHORTER)
    assert revised.draft is not None

    assert (await service.accept(first_id, OWNER_ID)).outcome is DraftOutcome.STALE
    assert table(repository, "SELECT COUNT(*) FROM publication_outbox") == [(0,)]
    approved = await service.accept(revised.draft.draft_id, OWNER_ID)
    assert approved.package is not None and approved.package.draft_id == revised.draft.draft_id


async def test_package_rules_live_in_the_domain(repository: SQLiteEventRepository) -> None:
    service = DraftService(repository=repository, writer=DeterministicDraftWriter())
    draft_id = await drafted(repository, service)
    draft = await repository.get_draft(draft_id)
    assert draft is not None
    card = await repository.get_scored_event(draft.event_id)
    assert card is not None
    now = datetime.now(UTC)

    package = build_publication_package(card.event, draft, approved_by=OWNER_ID, approved_at=now)
    assert package.idempotency_key == idempotency_key(card.event)
    with pytest.raises(ValidationError):
        package.model_validate(
            package.model_dump() | {"fact_check_status": FactCheckStatus.NEEDS_REVIEW}
        )
    notified = card.event.model_copy(update={"status": EventStatus.NOTIFIED})
    with pytest.raises(ValueError):
        build_publication_package(notified, draft, approved_by=OWNER_ID, approved_at=now)


async def test_telegram_accept_reports_the_outbox_and_disabled_publishing(
    repository: SQLiteEventRepository,
) -> None:
    gateway = FakeGateway()
    bot = telegram(repository, gateway)
    await seed(repository, x_item(1))
    await review_service(repository, gateway).deliver(urgent=False)
    inbox = Inbox()
    await bot.handle_callback(OWNER_ID, f"e:use:{gateway.cards[0][0].event.event_id}", inbox)
    draft_data = inbox.last.keyboard[0][0][1] if inbox.last.keyboard else ""

    await bot.handle_callback(OWNER_ID, draft_data, inbox)
    await bot.handle_callback(OWNER_ID, draft_data, inbox)
    await bot.handle_message(OWNER_ID, "/saved", inbox)

    texts = [reply.text for reply in inbox.replies]
    assert draft_data.startswith("d:ok:")
    assert any("outbox" in text and "выключена" in text for text in texts)
    assert "Материал уже одобрен, пакет в outbox уже есть." in texts
    assert "Одобрено, ждёт публикации (outbox): 1" in inbox.last.text
