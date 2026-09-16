import sqlite3
import time
from datetime import UTC, datetime, timedelta

from fakes import x_item

from qmemo_radar.application.filtering import FilterPolicy
from qmemo_radar.application.pipeline import PipelineThresholds, RadarPipeline
from qmemo_radar.infrastructure.collectors import FakeCollector
from qmemo_radar.infrastructure.ranking import DeterministicFixtureRanker
from qmemo_radar.infrastructure.storage import SQLiteEventRepository


async def test_only_untouched_noise_is_prunable(repository: SQLiteEventRepository) -> None:
    copied = "A plain roadmap update that nobody quoted anywhere this week."
    items = [
        x_item(1, "Too short"),  # FILTERED_OUT noise
        x_item(2, copied),  # ARCHIVED original of a stored copy: kept
        x_item(3, copied),  # FILTERED_OUT copy
        x_item(4, "Another plain roadmap update with no quotable sentence."),  # ARCHIVED
        x_item(5, "A third plain update that the owner still reacted to."),  # ARCHIVED + feedback
    ]
    await RadarPipeline(
        collector=FakeCollector(items),
        ranker=DeterministicFixtureRanker(),
        repository=repository,
        filter_policy=FilterPolicy(max_age=timedelta(hours=1)),
        thresholds=PipelineThresholds(archive=50, digest=90, urgent=95),
    ).run_once()
    with sqlite3.connect(repository._db_path) as db:
        db.execute(
            "INSERT INTO feedback (event_id, action, telegram_user_id, created_at) "
            "SELECT id, 'SKIP', 1, '2026-01-01' FROM radar_events WHERE external_id = '1005'"
        )
        statuses = dict(db.execute("SELECT external_id, status FROM radar_events"))
    assert statuses == {
        "1001": "FILTERED_OUT",
        "1002": "ARCHIVED",
        "1003": "FILTERED_OUT",
        "1004": "ARCHIVED",
        "1005": "ARCHIVED",
    }

    later = datetime.now(UTC) + timedelta(days=1)
    assert await repository.count_prunable_noise(later) == {"FILTERED_OUT": 2, "ARCHIVED": 1}
    assert await repository.count_prunable_noise(datetime.now(UTC) - timedelta(days=14)) == {}


async def test_prunable_count_stays_fast_on_a_large_table(
    repository: SQLiteEventRepository,
) -> None:
    rows = [
        (
            f"e{n}",
            "rss",
            str(n),
            f"https://news.example/{n}",
            "noise",
            "noise",
            f"h{n}",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "FILTERED_OUT",
            "2026-01-01",
            "2026-01-01",
            f"e{n - 1}" if n % 10 == 0 else None,  # every tenth row is a copy of the previous one
        )
        for n in range(1, 30_001)
    ]
    with sqlite3.connect(repository._db_path) as db:
        db.executemany(
            "INSERT INTO radar_events (id, source, external_id, url, original_text, "
            "normalized_text, content_hash, published_at, discovered_at, status, created_at, "
            "updated_at, duplicate_of_event_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )

    started = time.perf_counter()
    counts = await repository.count_prunable_noise(datetime(2026, 2, 1, tzinfo=UTC))
    elapsed = time.perf_counter() - started

    assert counts == {"FILTERED_OUT": 27_000}  # the 3,000 originals of copies are kept
    assert elapsed < 5  # without the 008 indexes this took minutes (a scan per candidate)
