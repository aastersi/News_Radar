from dataclasses import dataclass
from datetime import timedelta

import httpx

from qmemo_radar.application.filtering import FilterPolicy
from qmemo_radar.application.normalization import comparison_text
from qmemo_radar.application.pipeline import PipelineThresholds, RadarPipeline
from qmemo_radar.application.ports import Ranker, SourceCollector
from qmemo_radar.config import RadarSettings, SourcesConfig
from qmemo_radar.infrastructure.collectors.x_api import (
    X_API_BASE_URL,
    XApiClient,
    XQuery,
    XRecentSearchCollector,
)
from qmemo_radar.infrastructure.storage import SQLiteEventRepository


@dataclass(frozen=True, slots=True)
class Application:
    settings: RadarSettings
    repository: SQLiteEventRepository


def build_application(settings: RadarSettings | None = None) -> Application:
    resolved = settings or RadarSettings()
    resolved.ensure_data_directory()
    return Application(
        settings=resolved,
        repository=SQLiteEventRepository(resolved.db_path),
    )


def build_pipeline(
    application: Application,
    *,
    collector: SourceCollector,
    ranker: Ranker,
    sources: SourcesConfig | None = None,
) -> RadarPipeline:
    settings = application.settings
    resolved_sources = sources or SourcesConfig()
    return RadarPipeline(
        collector=collector,
        ranker=ranker,
        repository=application.repository,
        filter_policy=FilterPolicy(
            max_age=timedelta(minutes=settings.max_event_age_minutes),
            blocked_authors=frozenset(
                item.removeprefix("@").casefold() for item in resolved_sources.blocked_authors
            ),
            blocked_terms=frozenset(
                comparison_text(item) for item in resolved_sources.blocked_terms
            ),
        ),
        thresholds=PipelineThresholds(
            archive=settings.archive_threshold,
            digest=settings.digest_threshold,
            urgent=settings.urgent_threshold,
        ),
    )


def build_x_http_client(settings: RadarSettings) -> httpx.AsyncClient:
    if settings.x_bearer_token is None:
        raise ValueError("RADAR_X_BEARER_TOKEN is required")
    return httpx.AsyncClient(
        base_url=X_API_BASE_URL,
        headers={"Authorization": f"Bearer {settings.x_bearer_token.get_secret_value()}"},
        timeout=httpx.Timeout(20.0, connect=10.0),
    )


def build_x_collector(
    client: XApiClient,
    settings: RadarSettings,
    sources: SourcesConfig,
) -> XRecentSearchCollector:
    queries = [
        XQuery(f"account:{account.handle.casefold()}", f"from:{account.handle} -is:retweet")
        for account in sources.x.accounts
        if account.enabled
    ] + [XQuery(f"query:{query.name}", query.query) for query in sources.x.queries if query.enabled]
    return XRecentSearchCollector(
        client,
        queries,
        lookback=timedelta(minutes=settings.max_event_age_minutes),
        max_pages=sources.x.max_pages_per_query,
    )
