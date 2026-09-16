import asyncio
import json
import logging
import re
import socket
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
from qmemo_radar.application.scheduler import RadarScheduler, seconds_until_next
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
        "X: ошибки в 1 из 2 источников",
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

    assert seconds_until_next(datetime(2026, 9, 16, 2, 0, tzinfo=UTC), times, zone) == 3600
    assert seconds_until_next(datetime(2026, 9, 16, 3, 0, tzinfo=UTC), times, zone) == 5 * 3600
    late = datetime(2026, 9, 16, 13, 30, tzinfo=UTC)  # 20:30 local
    assert seconds_until_next(late, times, zone) == 13.5 * 3600


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
