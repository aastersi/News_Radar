"""Live smoke test of the free sources against the real internet. No keys, no paid API.

RADAR_LIVE_FREE_SOURCES=1 pytest tests/test_live_free_sources.py -s
"""

import os
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from qmemo_radar.application.budget import month_start
from qmemo_radar.application.collection import MultiSourceCollector
from qmemo_radar.application.filtering import FilterPolicy
from qmemo_radar.application.pipeline import PipelineThresholds, RadarPipeline
from qmemo_radar.bootstrap import build_free_http_client
from qmemo_radar.infrastructure.collectors.gdelt_gqg import SOURCE_KEY, GdeltQuotationCollector
from qmemo_radar.infrastructure.collectors.rss import Feed, RssCollector
from qmemo_radar.infrastructure.storage import SQLiteEventRepository

FEEDS = (
    Feed("bbc_world", "https://feeds.bbci.co.uk/news/world/rss.xml"),  # RSS 2.0
    Feed("npr_news", "https://feeds.npr.org/1001/rss.xml"),  # RSS 2.0
    Feed("verge", "https://www.theverge.com/rss/index.xml"),  # Atom
    Feed("hn_frontpage", "https://hnrss.org/frontpage"),  # RSS 2.0
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        os.environ.get("RADAR_LIVE_FREE_SOURCES") != "1",
        reason="set RADAR_LIVE_FREE_SOURCES=1 to read real GDELT and RSS feeds",
    ),
]


def count_rows(repository: SQLiteEventRepository) -> dict[str, object]:
    with sqlite3.connect(repository._db_path) as db:
        return {
            "rows": db.execute(
                "SELECT source, COUNT(*) FROM radar_events GROUP BY source"
            ).fetchall(),
            "duplicate_ids": db.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT source || char(0) || external_id)"
                " FROM radar_events"
            ).fetchone()[0],
            "duplicate_rss_urls": db.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT url) FROM radar_events WHERE source = 'rss'"
            ).fetchone()[0],
            "ledger": db.execute("SELECT COUNT(*) FROM cost_ledger").fetchone()[0],
        }


async def test_real_gdelt_and_rss_reach_sqlite_without_duplicates(
    repository: SQLiteEventRepository,
) -> None:
    started = datetime.now(UTC)
    async with build_free_http_client() as http:
        gdelt = GdeltQuotationCollector(
            http,
            safety_lag=timedelta(minutes=10),
            max_minutes_per_run=60,
            first_run_lookback=timedelta(minutes=45),  # 35 checked minutes: files and gaps
            languages=frozenset({"english"}),
            allow_unknown_language=False,
            clock=lambda: started,  # a rerun sees exactly the same minutes
        )
        rss = RssCollector(http, FEEDS, max_bytes=5_000_000)

        def pipeline() -> RadarPipeline:
            return RadarPipeline(
                collector=MultiSourceCollector({"gdelt_gqg": gdelt, "rss": rss}),
                ranker=None,
                repository=repository,
                filter_policy=FilterPolicy(max_age=timedelta(minutes=60)),
                thresholds=PipelineThresholds(),
            )

        first = await pipeline().run_once(run_id="live-1")
        after_first = count_rows(repository)
        cursor = (await repository.get_checkpoints())[SOURCE_KEY]
        second = await pipeline().run_once(run_id="live-2")  # same window, RSS validators sent
        with sqlite3.connect(repository._db_path) as db:
            db.execute("DELETE FROM source_checkpoints")  # checkpoint lost
        third = await pipeline().run_once(run_id="live-3")

    metrics = await repository.metrics_since(started - timedelta(minutes=1))
    after = count_rows(repository)
    print("\nfirst", first.model_dump(), "\nsecond", second.model_dump())
    print("third", third.model_dump(), "\nrows", after_first, "->", after)
    for key, values in sorted(metrics.items()):
        print(key, values)

    gdelt_stats = metrics[SOURCE_KEY]
    assert gdelt_stats["files_found"] >= 3  # three runs over the same minutes
    assert gdelt_stats["expected_gaps"] > 0 and gdelt_stats.get("source_errors", 0) == 0
    assert gdelt_stats["quotes_accepted"] > 0
    newest = (started - timedelta(minutes=10)).replace(second=0, microsecond=0)
    assert cursor == (newest + timedelta(minutes=1)).strftime("%Y%m%d%H%M%S")
    before, rows = dict(after_first["rows"]), dict(after["rows"])  # type: ignore[call-overload]
    assert before.get("gdelt", 0) > 0 and before.get("rss", 0) > 0
    # Reruns over the same GDELT minutes add nothing; a feed may only add a genuinely new entry.
    assert rows["gdelt"] == before["gdelt"] and third.inserted <= 5
    assert after["duplicate_ids"] == 0 and after["duplicate_rss_urls"] == 0
    assert after["ledger"] == 0
    assert await repository.cost_since(month_start(started)) == 0
    health = {item.source_key: item for item in await repository.source_health()}
    working = [feed for feed in FEEDS if health[feed.source_key].last_error is None]
    assert len(working) >= 2, {key: value.last_error for key, value in health.items()}
