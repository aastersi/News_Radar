import sqlite3
from datetime import timedelta

from fakes import (
    OWNER_ID,
    TIMEZONE,
    FakeGateway,
    FakeLookup,
    Inbox,
    review_service,
    seed,
    telegram,
    x_item,
)

from qmemo_radar.application.review import DeliveryLimits, Outcome
from qmemo_radar.domain import EventStatus
from qmemo_radar.infrastructure.storage import SQLiteEventRepository
from qmemo_radar.interfaces.telegram import render

STRANGER_ID = 999


def count(repository: SQLiteEventRepository, table: str) -> int:
    with sqlite3.connect(repository._db_path) as db:
        return int(db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


async def test_unauthorized_user_gets_no_state_and_changes_nothing(
    repository: SQLiteEventRepository,
) -> None:
    gateway = FakeGateway()
    await seed(repository, x_item(1))
    await review_service(repository, gateway).deliver(urgent=False)
    [(card, _, _)] = gateway.cards
    bot = telegram(repository, gateway)
    inbox = Inbox()

    for command in ("/status", "/today", "/saved", "/pause", "/run", "hello"):
        await bot.handle_message(STRANGER_ID, command, inbox)
    await bot.handle_message(None, "/today", inbox)
    await bot.handle_callback(STRANGER_ID, f"e:skip:{card.event.event_id}", inbox)
    await bot.handle_callback(STRANGER_ID, f"e:why:{card.event.event_id}", inbox)

    assert {reply.text for reply in inbox.replies} == {
        f"Нет доступа. Ваш Telegram ID: {STRANGER_ID}",
        "Нет доступа. Ваш Telegram ID: None",
    }
    assert all(reply.keyboard is None for reply in inbox.replies)
    assert await repository.count_by_status() == {EventStatus.NOTIFIED.value: 1}
    assert await repository.get_state("paused") is None
    assert count(repository, "feedback") == 0


async def test_callback_data_fits_64_bytes_and_keeps_the_full_id(
    repository: SQLiteEventRepository,
) -> None:
    await seed(repository, x_item(1))
    [card] = await repository.list_deliverable({EventStatus.SHORTLISTED}, min_total=0, limit=5)

    buttons = [button for row in render.card_keyboard(card.event.event_id) for button in row]

    assert [label for label, _ in buttons] == [
        "✅ Использовать",
        "⏭ Пропустить",
        "🕒 Позже",
        "❓ Почему такой балл?",
    ]
    for _, data in buttons:
        assert len(data.encode()) <= 64
        assert data.rsplit(":", 1)[1] == card.event.event_id


async def test_card_escapes_html_and_contains_required_parts(
    repository: SQLiteEventRepository,
) -> None:
    await seed(repository, x_item(1, 'CEO said: "<b>We</b> & you will win" <script>'))
    [card] = await repository.list_deliverable({EventStatus.SHORTLISTED}, min_total=0, limit=5)

    text = render.card_text(card, timezone=TIMEZONE, urgent=True)

    assert "<script>" not in text and "<b>We</b>" not in text
    assert "&lt;b&gt;We&lt;/b&gt; &amp; you" in text
    for part in ("Срочно", "Founder Name", "@founder", "Балл:", "Формат:", "Действие:", "⚠️"):
        assert part in text
    assert 'href="https://x.com/founder/status/1001"' in text
    assert card.event.published_at.astimezone(TIMEZONE).strftime("%d.%m %H:%M") in text


async def test_event_is_notified_only_after_message_id_is_saved_and_failed_send_is_retried(
    repository: SQLiteEventRepository,
) -> None:
    gateway = FakeGateway()
    service = review_service(repository, gateway)
    await seed(repository, x_item(1))

    gateway.fail = True
    assert await service.deliver(urgent=False) == 0
    assert await repository.count_by_status() == {EventStatus.SHORTLISTED.value: 1}
    assert count(repository, "telegram_deliveries") == 0

    gateway.fail = False
    assert await service.deliver(urgent=False) == 1
    assert await repository.count_by_status() == {EventStatus.NOTIFIED.value: 1}
    with sqlite3.connect(repository._db_path) as db:
        rows = db.execute("SELECT message_id FROM telegram_deliveries").fetchall()
    assert rows == [(gateway.cards[0][2],)]


async def test_repeated_delivery_does_not_send_a_second_card(
    repository: SQLiteEventRepository,
) -> None:
    gateway = FakeGateway()
    service = review_service(repository, gateway)
    await seed(repository, x_item(1))

    assert await service.deliver(urgent=True) == 1
    assert await service.deliver(urgent=True) == 0
    assert await service.deliver(urgent=False) == 0
    assert len(gateway.cards) == 1


async def test_digest_size_and_daily_limit(repository: SQLiteEventRepository) -> None:
    gateway = FakeGateway()
    service = review_service(repository, gateway)
    await seed(repository, *(x_item(number) for number in range(12)))

    sent = [await service.deliver(urgent=False) for _ in range(3)]
    await seed(repository, x_item(50))

    assert sent == [5, 5, 0]
    assert await service.deliver(urgent=True) == 0
    report = await service.today()
    assert (len(report.delivered), report.remaining, report.waiting) == (10, 0, 3)


async def test_urgent_delivery_uses_threshold(repository: SQLiteEventRepository) -> None:
    gateway = FakeGateway()
    service = review_service(repository, gateway)
    plain = "A company representative shared an update on the roadmap for next quarter today."
    await seed(repository, x_item(1), x_item(2, plain, replies=0))

    assert await service.deliver(urgent=True) == 1
    [(card, urgent, _)] = gateway.cards
    assert urgent and card.score.total >= 80
    assert await service.deliver(urgent=False) == 1


async def test_old_buttons_do_not_change_a_finished_decision(
    repository: SQLiteEventRepository,
) -> None:
    gateway = FakeGateway()
    await seed(repository, x_item(1))
    await review_service(repository, gateway).deliver(urgent=False)
    [(card, _, _)] = gateway.cards
    event_id = card.event.event_id
    bot = telegram(repository, gateway)
    inbox = Inbox()

    await bot.handle_callback(OWNER_ID, f"e:skip:{event_id}", inbox)
    await bot.handle_callback(OWNER_ID, f"e:later:{event_id}", inbox)
    await bot.handle_callback(OWNER_ID, f"e:skip:{event_id}", inbox)
    await bot.handle_callback(OWNER_ID, "e:skip:not-an-id", inbox)

    assert [reply.text for reply in inbox.replies] == [
        "⏭ Пропущено.",
        "Решение по этому событию уже принято, кнопка больше не действует.",
        "Решение по этому событию уже принято, кнопка больше не действует.",
        "Кнопка устарела.",
    ]
    assert await repository.count_by_status() == {EventStatus.SKIPPED.value: 1}
    assert count(repository, "feedback") == 1


async def test_later_returns_the_card_in_the_next_digest(
    repository: SQLiteEventRepository,
) -> None:
    gateway = FakeGateway()
    service = review_service(repository, gateway)
    await seed(repository, x_item(1))
    await service.deliver(urgent=False)
    event_id = gateway.cards[0][0].event.event_id

    assert await service.later(event_id, OWNER_ID) is Outcome.DONE
    assert [card.event.event_id for card in await service.snoozed()] == [event_id]
    assert await service.deliver(urgent=True) == 0
    assert await service.deliver(urgent=False) == 1
    assert len(gateway.cards) == 2


async def test_pause_stops_deliveries_and_why_explains_the_score(
    repository: SQLiteEventRepository,
) -> None:
    gateway = FakeGateway()
    bot = telegram(repository, gateway)
    service = review_service(repository, gateway)
    await seed(repository, x_item(1))
    inbox = Inbox()

    await bot.handle_message(OWNER_ID, "/pause", inbox)
    assert await service.deliver(urgent=False) == 0
    await bot.handle_message(OWNER_ID, "/resume", inbox)
    assert await service.deliver(urgent=False) == 1
    await bot.handle_callback(OWNER_ID, f"e:why:{gateway.cards[0][0].event.event_id}", inbox)

    assert inbox.replies[0].text == "Radar поставлен на паузу."
    assert "Связь с QMemo: 27/30" in inbox.last.text
    assert "Итог считает код" in inbox.last.text


async def test_manual_link_is_validated_before_any_lookup(
    repository: SQLiteEventRepository,
) -> None:
    lookup = FakeLookup(x_item(7, 'Manual pick: "This quote was sent by hand."', minutes_ago=600))
    service = review_service(repository, FakeGateway(), lookup=lookup)

    assert await service.submit_link("https://evil.example/founder/status/1") is Outcome.INVALID
    assert lookup.calls == []
    assert await service.submit_link("https://x.com/founder/status/1007?s=20") is Outcome.DONE
    assert await service.submit_link("https://x.com/founder/status/1007") is Outcome.ALREADY_DECIDED
    assert lookup.calls == ["1007", "1007"]

    await seed(repository)
    [card] = await repository.list_deliverable({EventStatus.SHORTLISTED}, min_total=100, limit=5)
    assert card.event.source_key == "manual"


async def test_expire_moves_stale_events_out_of_review(repository: SQLiteEventRepository) -> None:
    service = review_service(
        repository, FakeGateway(), limits=DeliveryLimits(event_ttl=timedelta(hours=-1))
    )
    await seed(repository, x_item(1))

    assert await service.expire() == 1
    assert await repository.count_by_status() == {EventStatus.EXPIRED.value: 1}
