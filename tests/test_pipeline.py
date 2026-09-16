from datetime import UTC, datetime, timedelta

import pytest

from qmemo_radar.application.filtering import FilterPolicy
from qmemo_radar.application.normalization import build_candidate
from qmemo_radar.application.pipeline import PipelineThresholds, RadarPipeline
from qmemo_radar.domain import Engagement, EventStatus, RawSourceItem, SourceType
from qmemo_radar.infrastructure.collectors import FakeCollector
from qmemo_radar.infrastructure.ranking import DeterministicFixtureRanker
from qmemo_radar.infrastructure.storage import SQLiteEventRepository


@pytest.mark.asyncio
async def test_pipeline_is_idempotent(tmp_path) -> None:
    repository = SQLiteEventRepository(tmp_path / "radar.db")
    await repository.initialize()
    item = RawSourceItem(
        source=SourceType.X,
        external_id="x-100",
        url="https://x.com/founder/status/100",
        author_handle="founder",
        original_text='Founder said: "This prediction will matter next year."',
        published_at=datetime.now(UTC),
        engagement=Engagement(replies=40),
    )
    pipeline = RadarPipeline(
        collector=FakeCollector([item]),
        ranker=DeterministicFixtureRanker(),
        repository=repository,
        filter_policy=FilterPolicy(max_age=timedelta(hours=1)),
        thresholds=PipelineThresholds(),
    )

    first = await pipeline.run_once()
    second = await pipeline.run_once()
    statuses = await repository.count_by_status()

    assert first.inserted == 1
    assert first.shortlisted == 1
    assert second.duplicates == 1
    assert statuses == {EventStatus.SHORTLISTED.value: 1}


@pytest.mark.asyncio
async def test_old_event_is_filtered_without_ranking(tmp_path) -> None:
    repository = SQLiteEventRepository(tmp_path / "radar.db")
    await repository.initialize()
    item = RawSourceItem(
        source=SourceType.X,
        external_id="x-old",
        url="https://x.com/founder/status/old",
        original_text="An old but otherwise meaningful publication for the radar.",
        published_at=datetime.now(UTC) - timedelta(hours=2),
    )
    pipeline = RadarPipeline(
        collector=FakeCollector([item]),
        ranker=DeterministicFixtureRanker(),
        repository=repository,
        filter_policy=FilterPolicy(max_age=timedelta(hours=1)),
        thresholds=PipelineThresholds(),
    )

    result = await pipeline.run_once()

    assert result.filtered == 1
    assert result.scored == 0
    assert await repository.count_by_status() == {EventStatus.FILTERED_OUT.value: 1}


@pytest.mark.asyncio
async def test_pipeline_recovers_discovered_event_after_interruption(tmp_path) -> None:
    repository = SQLiteEventRepository(tmp_path / "radar.db")
    await repository.initialize()
    item = RawSourceItem(
        source=SourceType.X,
        external_id="x-recovery",
        url="https://x.com/founder/status/recovery",
        original_text='Founder said: "A recoverable event must not be lost."',
        published_at=datetime.now(UTC),
        engagement=Engagement(replies=30),
    )
    assert await repository.add_event(build_candidate(item)) is True

    pipeline = RadarPipeline(
        collector=FakeCollector([item]),
        ranker=DeterministicFixtureRanker(),
        repository=repository,
        filter_policy=FilterPolicy(max_age=timedelta(hours=1)),
        thresholds=PipelineThresholds(),
    )

    result = await pipeline.run_once()

    assert result.duplicates == 1
    assert result.scored == 1
    assert await repository.count_by_status() == {EventStatus.SHORTLISTED.value: 1}
