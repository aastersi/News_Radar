import asyncio
import json
import logging
import re
import signal
import socket
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fakes import (
    OWNER_ID,
    TIMEZONE,
    FakeGateway,
    FakeLookup,
    Inbox,
    settings_for,
    telegram,
    x_item,
)
from pydantic import SecretStr

from qmemo_radar.application.ports import SourceCollector
from qmemo_radar.application.runner import HEARTBEAT_KEY
from qmemo_radar.application.scheduler import RadarScheduler, next_occurrence
from qmemo_radar.bootstrap import (
    Application,
    JsonLogFormatter,
    Services,
    build_publishers,
    build_services,
)
from qmemo_radar.config import RadarSettings, SourcesConfig
from qmemo_radar.domain import (
    Engagement,
    EventStatus,
    PublicationPackage,
    RawSourceItem,
    RunStatus,
    SourceFetch,
)
from qmemo_radar.exceptions import ProductionAdapterNotConfigured, PublishingDisabled
from qmemo_radar.infrastructure.collectors import FakeCollector
from qmemo_radar.infrastructure.drafting import DeterministicDraftWriter
from qmemo_radar.infrastructure.publishing import DisabledQuotePublisher, DisabledXPublisher
from qmemo_radar.infrastructure.ranking import DeterministicFixtureRanker
from qmemo_radar.infrastructure.storage import SQLiteEventRepository
from qmemo_radar.interfaces.cli import execute
from qmemo_radar.interfaces.runtime import serve

SRC = Path(__file__).parents[1] / "src" / "qmemo_radar"


def services(
    repository: SQLiteEventRepository, collector: SourceCollector | None = None
) -> Services:
    return build_services(
        Application(settings=settings_for(repository), repository=repository),
        collector=collector or FakeCollector([x_item(1)]),
        ranker=DeterministicFixtureRanker(),
        writer=DeterministicDraftWriter(),
        gateway=FakeGateway(),
        lookup=None,
        sources=SourcesConfig(),
    )


class SlowCollector:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.calls = 0

    async def collect(self, checkpoints: Mapping[str, str]) -> list[SourceFetch]:
        self.calls += 1
        await self.release.wait()
        return [SourceFetch(source_key="slow", items=(x_item(1),))]


class BrokenCollector:
    async def collect(self, checkpoints: Mapping[str, str]) -> list[SourceFetch]:
        raise RuntimeError("database is locked")


async def test_scheduled_and_manual_runs_share_one_lock(repository: SQLiteEventRepository) -> None:
    collector = SlowCollector()
    radar = services(repository, collector)
    bot = telegram(repository, FakeGateway())
    bot._runner = radar.runner  # the Telegram command must use the same runner instance
    inbox = Inbox()

    scheduled = asyncio.create_task(radar.runner.run_cycle(manual=False))
    await asyncio.sleep(0)
    await bot.handle_message(OWNER_ID, "/run", inbox)
    collector.release.set()
    result = await scheduled

    assert inbox.last.text == "Сбор не запущен: сбор уже идёт."
    assert collector.calls == 1
    assert result.status is RunStatus.SUCCESS and result.urgent_sent == 1


async def test_pause_skips_scheduled_cycles_but_not_manual_ones(
    repository: SQLiteEventRepository,
) -> None:
    radar = services(repository)
    await radar.review.set_paused(True)

    skipped = await radar.runner.run_cycle(manual=False)
    manual = await radar.runner.run_cycle(manual=True)

    assert skipped.skipped == "paused"
    assert manual.status is RunStatus.SUCCESS
    assert manual.urgent_sent == 0  # paused Radar sends no cards


async def test_failed_cycle_is_recorded_and_interrupted_runs_are_closed(
    repository: SQLiteEventRepository,
) -> None:
    radar = services(repository, BrokenCollector())

    result = await radar.runner.run_cycle(manual=True)
    await repository.start_run("crashed-mid-run")
    closed = await repository.fail_interrupted_runs()
    status = await radar.runner.status()

    assert result.status is RunStatus.FAILED
    assert closed == 1
    assert status.last_run is not None and status.last_run.status is RunStatus.FAILED
    assert status.last_success is None


async def test_status_command_speaks_human_language(repository: SQLiteEventRepository) -> None:
    gateway = FakeGateway()
    bot = telegram(repository, gateway, collector=FakeCollector([x_item(1)]))
    inbox = Inbox()
    await bot.handle_message(OWNER_ID, "/run", inbox)
    await repository.record_source_result(
        "account:broken", cursor=None, error_code="client_error_401"
    )

    await bot.handle_message(OWNER_ID, "/status", inbox)

    text = inbox.last.text
    assert "Сбор завершён:</b> SUCCESS" in inbox.replies[1].text
    for part in (
        "Работает:",
        "Пауза: выключена",
        "Последний сбор:",
        "Последний успешный сбор:",
        "Источники: ошибки в 1 из 2",
        "account:broken: client_error_401",
        "LLM: в порядке",
        "Сегодня: найдено 1 · отфильтровано 0 · оценено 1 · отправлено 1",
        "Outbox APPROVED: 0",
        "Публикация в QMemo: выключена · в X: выключена",
    ):
        assert part in text, part


def test_next_digest_time_uses_the_configured_local_schedule() -> None:
    zone = ZoneInfo("Asia/Ho_Chi_Minh")  # UTC+7
    times = [time(10), time(15), time(20)]

    def utc(day: int, hour: int, minute: int = 0) -> datetime:
        return datetime(2026, 9, day, hour, minute, tzinfo=UTC)

    assert next_occurrence(utc(16, 2), times, zone) == utc(16, 3)
    assert next_occurrence(utc(16, 3), times, zone) == utc(16, 8)  # strictly after the slot
    assert next_occurrence(utc(16, 13, 30), times, zone) == utc(17, 3)


def test_digest_schedule_survives_daylight_saving_changes() -> None:
    berlin = ZoneInfo("Europe/Berlin")  # clocks jump from 02:00 to 03:00 on 2026-03-29
    before_jump = datetime(2026, 3, 29, 0, 30, tzinfo=berlin)

    at = next_occurrence(before_jump, [time(9)], berlin)

    assert at == datetime(2026, 3, 29, 7, 0, tzinfo=UTC)  # 09:00 CEST


async def test_scheduler_runs_collection_expiry_and_heartbeat_then_stops(
    repository: SQLiteEventRepository,
) -> None:
    radar = services(repository)
    sleeps: list[float] = []
    never = asyncio.Event()

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        await never.wait()

    scheduler = RadarScheduler(
        runner=radar.runner,
        review=radar.review,
        collect_every=timedelta(minutes=30),
        digest_times=[time(10), time(15)],
        timezone=TIMEZONE,
        sleep=sleep,
    )
    task = asyncio.create_task(scheduler.run())
    for _ in range(500):  # wait until every loop finished its first run and went to sleep
        await asyncio.sleep(0.01)
        if len(sleeps) == 4:
            break
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(sleeps) == 4 and {60.0, 1800.0, 3600.0} <= set(sleeps)
    assert await repository.get_state(HEARTBEAT_KEY) is not None
    assert (await repository.last_run()) is not None


async def test_serve_stops_on_signal_event_and_cancels_jobs() -> None:
    stop = asyncio.Event()
    cleaned: list[str] = []

    async def job(name: str) -> None:
        try:
            await asyncio.Event().wait()
        finally:
            cleaned.append(name)

    runner = asyncio.create_task(serve([lambda: job("scheduler"), lambda: job("poller")], stop))
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(runner, 1)

    assert sorted(cleaned) == ["poller", "scheduler"]


async def test_serve_lets_polling_stop_gracefully_before_cancelling() -> None:
    stop = asyncio.Event()
    polling_stopped = asyncio.Event()
    order: list[str] = []

    async def poller() -> None:
        await polling_stopped.wait()
        order.append("poller finished by itself")

    async def scheduler() -> None:
        try:
            await asyncio.Event().wait()
        finally:
            order.append("scheduler cancelled")

    async def stop_polling() -> None:
        polling_stopped.set()
        await asyncio.sleep(0.01)
        order.append("stop_polling returned")

    runner = asyncio.create_task(serve([scheduler, poller], stop, on_stop=stop_polling))
    await asyncio.sleep(0.01)
    stop.set()
    await asyncio.wait_for(runner, 1)

    assert order == ["poller finished by itself", "stop_polling returned", "scheduler cancelled"]


async def test_dispatcher_shutdown_waits_for_started_handlers() -> None:
    from aiogram import Bot
    from aiogram.types import Chat, Message, Update, User

    from qmemo_radar.interfaces.telegram.bot import build_dispatcher

    release = asyncio.Event()
    handled: list[str] = []

    class SlowController:
        async def handle_message(self, user_id: int | None, text: str, send: object) -> None:
            await release.wait()
            handled.append(text)

    dispatcher = build_dispatcher(SlowController())  # type: ignore[arg-type]
    bot = Bot("123456:TEST")
    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=OWNER_ID, type="private"),
        from_user=User(id=OWNER_ID, is_bot=False, first_name="Owner"),
        text="/run",
    )
    handler = asyncio.create_task(dispatcher.feed_update(bot, Update(update_id=1, message=message)))
    await asyncio.sleep(0.01)

    shutdown = asyncio.create_task(dispatcher.emit_shutdown(bot=bot))
    await asyncio.sleep(0.01)
    assert not shutdown.done()
    release.set()
    await asyncio.wait_for(shutdown, 1)
    await handler
    await bot.session.close()

    assert handled == ["/run"]


async def test_serve_fails_when_a_job_dies() -> None:
    async def crash() -> None:
        raise ValueError("poller crashed")

    async def forever() -> None:
        await asyncio.Event().wait()

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(serve([crash, forever], asyncio.Event()), 1)


def test_empty_values_from_env_example_mean_not_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    example = (Path(__file__).parents[1] / ".env.example").read_text(encoding="utf-8")
    (tmp_path / ".env").write_text(example, encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    settings = RadarSettings()

    assert settings.allowed_telegram_id is None
    assert settings.qmemo_publishing_enabled is False and settings.x_publishing_enabled is False
    assert "RADAR_ALLOWED_TELEGRAM_ID is required" in settings.production_problems()


def test_digest_times_are_validated() -> None:
    assert RadarSettings(_env_file=None, digest_times="20:00, 09:30").digest_schedule == (
        time(9, 30),
        time(20),
    )
    for invalid in ("10:00", "10:00,11:00,12:00,13:00", "ten,eleven"):
        with pytest.raises(ValueError):
            RadarSettings(_env_file=None, digest_times=invalid)


async def test_only_disabled_publishers_exist_and_flags_fail_closed(tmp_path: Path) -> None:
    quote, x = build_publishers(RadarSettings(_env_file=None))
    assert isinstance(quote, DisabledQuotePublisher) and isinstance(x, DisabledXPublisher)
    package = PublicationPackage.model_construct(package_id="p1")
    with pytest.raises(PublishingDisabled):
        await quote.publish(package)
    with pytest.raises(PublishingDisabled):
        await x.publish(package, "https://qmemo.example/q/1")

    for flag in ("qmemo_publishing_enabled", "x_publishing_enabled"):
        enabled = RadarSettings(_env_file=None, **{flag: True})  # type: ignore[arg-type]
        with pytest.raises(ProductionAdapterNotConfigured):
            build_publishers(enabled)
        assert any("must stay false" in problem for problem in enabled.production_problems())


def test_no_code_can_send_a_publication_request() -> None:
    posts = {
        path.relative_to(SRC).as_posix()
        for path in SRC.rglob("*.py")
        if re.search(r'"POST"|\.post\(', path.read_text(encoding="utf-8"))
    }
    x_calls = re.findall(
        r'send_with_retry\(\s*self\._http,\s*"(\w+)"',
        (SRC / "infrastructure" / "collectors" / "x_api.py").read_text(encoding="utf-8"),
    )

    assert posts == {"infrastructure/llm.py"}  # the only POST is the LLM chat completion
    assert x_calls == ["GET"]
    assert not list(SRC.rglob("*quote_memorial*")) and not list(SRC.rglob("*qmemo_api*"))


def test_tests_cannot_reach_the_network() -> None:
    with pytest.raises(RuntimeError, match="must not open network connections"):
        socket.create_connection(("203.0.113.10", 443), timeout=1)


def test_json_logs_have_structured_fields_and_redact_secrets() -> None:
    secret = "123456:telegram-secret-token"
    record = logging.LogRecord("qmemo_radar.test", logging.INFO, "", 0, "sent %s", (secret,), None)
    record.run_id, record.event_id, record.operation, record.result = "r1", "e1", "deliver", "ok"
    record.error_code = "none"

    line = JsonLogFormatter([secret]).format(record)
    entry = json.loads(line)

    assert secret not in line
    assert entry["message"] == "sent [redacted]"
    assert {k: entry[k] for k in ("run_id", "event_id", "module", "operation", "result")} == {
        "run_id": "r1",
        "event_id": "e1",
        "module": "qmemo_radar.test",
        "operation": "deliver",
        "result": "ok",
    }
    assert RadarSettings(_env_file=None, telegram_bot_token=SecretStr(secret)).secret_values() == [
        secret
    ]


async def test_healthcheck_reads_the_heartbeat_without_creating_a_database(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "radar.db"
    assert await execute("healthcheck", db_path=db) == 1
    assert not db.exists()

    repository = SQLiteEventRepository(db)
    await repository.initialize()
    await repository.set_state(HEARTBEAT_KEY, datetime.now(UTC).isoformat())
    assert await execute("healthcheck", db_path=db) == 0
    stale = (datetime.now(UTC) - timedelta(minutes=10)).isoformat()
    await repository.set_state(HEARTBEAT_KEY, stale)
    assert await execute("healthcheck", db_path=db) == 1
    capsys.readouterr()


async def test_cli_dry_run_proves_the_offline_path_and_survives_restart(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db = tmp_path / "radar.db"

    assert await execute("dry-run", db_path=db) == 0
    first = json.loads(capsys.readouterr().out)
    assert await execute("dry-run", db_path=db) == 0
    second = json.loads(capsys.readouterr().out)

    assert first["run_status"] == "SUCCESS" and first["package_id"]
    assert first["cards_sent"] == {"urgent": 1, "digest": 1}
    assert first["counters"]["filtered"] == 1
    assert second["counters"]["duplicates"] == 3 and second["counters"]["inserted"] == 0
    assert second["package_id"] is None and second["outbox_approved"] == 1
    assert first["qmemo_publishing"] is False and first["x_publishing"] is False


async def test_manual_link_skips_age_limit_and_reaches_the_owner(
    repository: SQLiteEventRepository,
) -> None:
    old_post = RawSourceItem.model_validate(
        x_item(
            9, 'Old but chosen: "Manual picks are always reviewed."', minutes_ago=600
        ).model_dump()
        | {"engagement": Engagement()}
    )
    gateway = FakeGateway()
    bot = telegram(repository, gateway, lookup=FakeLookup(old_post))
    inbox = Inbox()

    await bot.handle_message(OWNER_ID, "https://x.com/founder/status/1009", inbox)
    await bot.handle_message(OWNER_ID, "/run", inbox)

    assert inbox.replies[0].text == "Ссылка добавлена. Она будет оценена при следующем сборе."
    assert [card.event.source_key for card, _, _ in gateway.cards] == ["manual"]
    assert await repository.count_by_status() == {EventStatus.NOTIFIED.value: 1}


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals; runs in CI and Docker")
def test_sigterm_stops_the_process_gracefully(tmp_path: Path) -> None:
    script = tmp_path / "radar_process.py"
    script.write_text(
        """
import asyncio
from qmemo_radar.interfaces.runtime import install_signal_handlers, serve

async def job(name):
    try:
        print("started", name, flush=True)
        await asyncio.Event().wait()
    finally:
        print("closed", name, flush=True)

async def main():
    stop = asyncio.Event()
    install_signal_handlers(stop)
    await serve([lambda: job("scheduler"), lambda: job("poller")], stop)
    print("shutdown complete", flush=True)

asyncio.run(main())
""",
        encoding="utf-8",
    )
    process = subprocess.Popen([sys.executable, str(script)], stdout=subprocess.PIPE, text=True)
    assert process.stdout is not None
    started = {process.stdout.readline().strip(), process.stdout.readline().strip()}
    process.send_signal(signal.SIGTERM)
    output, _ = process.communicate(timeout=10)

    assert started == {"started scheduler", "started poller"}
    assert process.returncode == 0
    assert "closed scheduler" in output and "closed poller" in output
    assert output.strip().endswith("shutdown complete")


async def test_manual_link_revives_an_archived_post(repository: SQLiteEventRepository) -> None:
    plain = x_item(5, "A calm update about a routine product meeting held this afternoon.")
    await services(repository, FakeCollector([plain])).runner.run_cycle(manual=False)
    with sqlite3.connect(repository._db_path) as db:  # as if it had scored below the threshold
        db.execute("UPDATE radar_events SET status = 'ARCHIVED'")
    assert await repository.count_by_status() == {EventStatus.ARCHIVED.value: 1}
    gateway = FakeGateway()
    bot = telegram(repository, gateway, lookup=FakeLookup(plain))
    inbox = Inbox()

    await bot.handle_message(OWNER_ID, "https://x.com/founder/status/1005", inbox)
    await bot.handle_message(OWNER_ID, "/run", inbox)

    assert inbox.replies[0].text == "Ссылка добавлена. Она будет оценена при следующем сборе."
    assert [card.event.external_id for card, _, _ in gateway.cards] == ["1005"]


async def test_undeliverable_card_does_not_block_the_rest(
    repository: SQLiteEventRepository,
) -> None:
    from fakes import review_service, seed

    gateway = FakeGateway()
    gateway.rejected_external_ids = {"1001"}
    await seed(repository, x_item(1), x_item(2))

    sent = await review_service(repository, gateway).deliver(urgent=False)

    assert sent == 1 and gateway.cards[0][0].event.external_id == "1002"
    assert await repository.count_by_status() == {
        EventStatus.SHORTLISTED.value: 1,
        EventStatus.NOTIFIED.value: 1,
    }


async def test_expiry_during_ranking_does_not_fail_the_cycle(
    repository: SQLiteEventRepository,
) -> None:
    class ExpiringRanker(DeterministicFixtureRanker):
        async def rank(self, events):  # type: ignore[no-untyped-def]
            results = await super().rank(events)
            await repository.expire_events(datetime.now(UTC) + timedelta(hours=1))
            return results

    radar = build_services(
        Application(settings=settings_for(repository), repository=repository),
        collector=FakeCollector([x_item(1)]),
        ranker=ExpiringRanker(),
        writer=DeterministicDraftWriter(),
        gateway=FakeGateway(),
        lookup=None,
        sources=SourcesConfig(),
    )

    result = await radar.runner.run_cycle(manual=True)

    assert result.status is RunStatus.SUCCESS
    assert await repository.count_by_status() == {EventStatus.EXPIRED.value: 1}


async def test_link_like_text_is_never_used_as_a_revision_instruction(
    repository: SQLiteEventRepository,
) -> None:
    from fakes import review_service, seed

    gateway = FakeGateway()
    await seed(repository, x_item(1))
    await review_service(repository, gateway).deliver(urgent=False)
    bot = telegram(repository, gateway)
    inbox = Inbox()
    await bot.handle_callback(OWNER_ID, f"e:use:{gateway.cards[0][0].event.event_id}", inbox)

    await bot.handle_message(OWNER_ID, "https://twitter.com/founder/status/123", inbox)
    await bot.handle_message(OWNER_ID, "see x.com/founder/status/1", inbox)

    assert inbox.replies[-1].text == "Нужна ссылка вида https://x.com/имя/status/123."
    assert inbox.replies[-2].text == inbox.replies[-1].text
    assert await repository.latest_revisable_draft() is not None
