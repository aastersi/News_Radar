"""Source registry: independent collectors, isolated failures, own checkpoints, no duplicates."""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx
import pytest

from qmemo_radar.application.budget import BudgetGuard, PaidFeature
from qmemo_radar.application.collection import MultiSourceCollector
from qmemo_radar.application.filtering import FilterPolicy
from qmemo_radar.application.pipeline import PipelineThresholds, RadarPipeline
from qmemo_radar.application.ports import SourceCollector
from qmemo_radar.bootstrap import (
    SOURCE_REGISTRY,
    SourceContext,
    SourceRegistration,
    build_collector,
    enabled_sources,
)
from qmemo_radar.config import RadarSettings, SourcesConfig
from qmemo_radar.domain import CostEntry, RawSourceItem, SourceFetch, SourceType
from qmemo_radar.infrastructure.collectors.x_api import XApiClient
from qmemo_radar.infrastructure.ranking import DeterministicFixtureRanker
from qmemo_radar.infrastructure.storage import SQLiteEventRepository

X_SOURCES = SourcesConfig.model_validate(
    {"x": {"accounts": [{"handle": "founder"}], "queries": [{"name": "q", "query": "q"}]}}
)
X_ON = {"paid_sources_enabled": True, "x_paid_search_enabled": True}


def article(source: SourceType, number: int) -> RawSourceItem:
    return RawSourceItem(
        source=source,
        external_id=f"{source.value}-{number}",
        url=f"https://news.example/{source.value}/{number}",
        author_display_name="Newsroom",
        original_text=f'The minister said: "Budget item {number} of {source.value} is final."',
        published_at=datetime.now(UTC) - timedelta(minutes=5),
    )


class Feed:
    """A free source that, like an RSS feed, returns its whole window on every poll."""

    def __init__(self, key: str, items: list[RawSourceItem]) -> None:
        self.key = key
        self.items = items
        self.seen: list[str | None] = []

    async def collect(self, checkpoints: Mapping[str, str]) -> list[SourceFetch]:
        self.seen.append(checkpoints.get(self.key))
        cursor = str(len(self.items))
        return [SourceFetch(source_key=self.key, items=tuple(self.items), cursor=cursor)]


class Crashing:
    async def collect(self, checkpoints: Mapping[str, str]) -> list[SourceFetch]:
        raise RuntimeError("parser bug")


class Reporting:
    async def collect(self, checkpoints: Mapping[str, str]) -> list[SourceFetch]:
        return [SourceFetch(source_key="bluesky:firehose", error_code="network_error")]


def pipeline(collector: SourceCollector, repository: SQLiteEventRepository) -> RadarPipeline:
    return RadarPipeline(
        collector=collector,
        ranker=DeterministicFixtureRanker(),
        repository=repository,
        filter_policy=FilterPolicy(max_age=timedelta(hours=1)),
        thresholds=PipelineThresholds(),
    )


def settings(**values: object) -> RadarSettings:
    return RadarSettings(_env_file=None, **values)  # type: ignore[arg-type]


async def test_collectors_run_together_fail_alone_and_keep_their_own_checkpoints(
    repository: SQLiteEventRepository,
) -> None:
    rss = Feed("rss:wire", [article(SourceType.RSS, n) for n in range(3)])
    gdelt = Feed("gdelt:gqg", [article(SourceType.GDELT, n) for n in range(2)])
    collector = MultiSourceCollector(
        {"rss": rss, "broken": Crashing(), "gdelt": gdelt, "bluesky": Reporting()}
    )

    first = await pipeline(collector, repository).run_once()
    second = await pipeline(collector, repository).run_once()

    assert (first.collected, first.inserted, first.source_errors) == (5, 5, 2)
    assert (second.collected, second.inserted, second.duplicates) == (5, 0, 5)
    assert await repository.get_checkpoints() == {"rss:wire": "3", "gdelt:gqg": "2"}
    assert rss.seen == [None, "3"] and gdelt.seen == [None, "2"]
    health = {item.source_key: item for item in await repository.source_health()}
    assert health["broken"].last_error == "collector_failed:RuntimeError"
    assert health["broken"].consecutive_failures == 2
    assert health["bluesky:firehose"].last_error == "network_error"
    assert health["rss:wire"].consecutive_failures == 0
    assert sum((await repository.count_by_status()).values()) == 5


def test_disabled_x_is_never_built_and_needs_no_credentials() -> None:
    assert enabled_sources(settings(), X_SOURCES) == []
    # Paid flags off: no token, no client, and building the registry still succeeds.
    assert build_collector(SourceContext(settings(), X_SOURCES, x_client=None)).names == []
    # Flags on but nothing configured in sources.yaml: still disabled.
    assert enabled_sources(settings(**X_ON), SourcesConfig()) == []

    assert enabled_sources(settings(**X_ON), X_SOURCES) == ["x_search"]
    with pytest.raises(ValueError, match="RADAR_X_BEARER_TOKEN"):
        build_collector(SourceContext(settings(**X_ON), X_SOURCES, x_client=None))


async def test_free_collector_keeps_working_when_the_budget_is_exhausted(
    repository: SQLiteEventRepository,
) -> None:
    since = datetime(2000, 1, 1, tzinfo=UTC)
    full = CostEntry(
        provider="x",
        operation="earlier",
        units=2000,
        estimated_cost_usd=Decimal(10),
        created_at=datetime.now(UTC),
    )
    assert await repository.reserve_cost(full, since=since, limit_usd=Decimal(10))
    requests: list[httpx.Request] = []
    x_http = httpx.AsyncClient(
        base_url="https://api.x.com",
        transport=httpx.MockTransport(lambda r: requests.append(r) or httpx.Response(200)),
    )
    guard = BudgetGuard(
        repository,
        enabled=frozenset(PaidFeature),
        hard_limit_usd=Decimal(10),
        target_usd=Decimal(0),
    )
    rss = Feed("rss:wire", [article(SourceType.RSS, 1)])
    registry = (
        *SOURCE_REGISTRY,
        SourceRegistration("rss", lambda _settings, _sources: True, lambda _context: rss),
    )
    context = SourceContext(settings(**X_ON), X_SOURCES, XApiClient(x_http, guard=guard))

    counters = await pipeline(build_collector(context, registry), repository).run_once()

    assert counters.inserted == 1 and counters.source_errors == 2
    assert requests == []
    health = {item.source_key: item.last_error for item in await repository.source_health()}
    assert health == {
        "account:founder": "hard_limit_reached",
        "query:q": "hard_limit_reached",
        "rss:wire": None,
    }
