"""Composition root: the only place where concrete adapters are chosen and connected."""

import json
import logging
import sys
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import httpx

from qmemo_radar.application.drafting import DraftService
from qmemo_radar.application.filtering import FilterPolicy
from qmemo_radar.application.normalization import comparison_text
from qmemo_radar.application.pipeline import PipelineThresholds, RadarPipeline
from qmemo_radar.application.ports import (
    DraftWriter,
    PostLookup,
    QuotePublisher,
    Ranker,
    ReviewGateway,
    SourceCollector,
    XPublisher,
)
from qmemo_radar.application.review import DeliveryLimits, ReviewService
from qmemo_radar.application.runner import RadarRunner
from qmemo_radar.application.scheduler import RadarScheduler
from qmemo_radar.config import RadarSettings, SourcesConfig
from qmemo_radar.exceptions import ProductionAdapterNotConfigured
from qmemo_radar.infrastructure.collectors.x_api import (
    X_API_BASE_URL,
    XApiClient,
    XQuery,
    XRecentSearchCollector,
)
from qmemo_radar.infrastructure.drafting import LlmDraftWriter
from qmemo_radar.infrastructure.llm import ChatCompletionsClient
from qmemo_radar.infrastructure.publishing import DisabledQuotePublisher, DisabledXPublisher
from qmemo_radar.infrastructure.ranking import LlmRanker
from qmemo_radar.infrastructure.storage import SQLiteEventRepository

if TYPE_CHECKING:
    from aiogram import Bot, Dispatcher

    from qmemo_radar.interfaces.telegram.controller import TelegramController

_LOG_FIELDS = ("run_id", "event_id", "source_key", "operation", "result", "error_code")


@dataclass(frozen=True, slots=True)
class Application:
    settings: RadarSettings
    repository: SQLiteEventRepository


@dataclass(frozen=True, slots=True)
class Services:
    pipeline: RadarPipeline
    review: ReviewService
    drafts: DraftService
    runner: RadarRunner


@dataclass(frozen=True, slots=True)
class Runtime:
    repository: SQLiteEventRepository
    services: Services
    scheduler: RadarScheduler
    controller: "TelegramController"
    bot: "Bot"
    dispatcher: "Dispatcher"
    quote_publisher: QuotePublisher
    x_publisher: XPublisher


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


def build_services(
    application: Application,
    *,
    collector: SourceCollector,
    ranker: Ranker,
    writer: DraftWriter,
    gateway: ReviewGateway,
    lookup: PostLookup | None,
    sources: SourcesConfig,
) -> Services:
    settings = application.settings
    repository = application.repository
    pipeline = build_pipeline(application, collector=collector, ranker=ranker, sources=sources)
    review = ReviewService(
        repository=repository,
        gateway=gateway,
        chat_id=settings.allowed_telegram_id or 0,
        limits=DeliveryLimits(
            daily_cards=settings.daily_card_limit,
            digest_cards=settings.digest_card_limit,
            urgent_threshold=settings.urgent_threshold,
            event_ttl=timedelta(hours=settings.event_ttl_hours),
        ),
        timezone=settings.zone,
        lookup=lookup,
    )
    return Services(
        pipeline=pipeline,
        review=review,
        drafts=DraftService(repository=repository, writer=writer),
        runner=RadarRunner(
            pipeline=pipeline,
            repository=repository,
            review=review,
            timezone=settings.zone,
            qmemo_publishing_enabled=settings.qmemo_publishing_enabled,
            x_publishing_enabled=settings.x_publishing_enabled,
        ),
    )


def build_publishers(settings: RadarSettings) -> tuple[QuotePublisher, XPublisher]:
    """Only the disabled publishers exist. Enabling a flag fails closed instead of publishing."""
    if settings.qmemo_publishing_enabled or settings.x_publishing_enabled:
        raise ProductionAdapterNotConfigured("No real Quote Memorial or X publisher is registered")
    return DisabledQuotePublisher(), DisabledXPublisher()


@asynccontextmanager
async def build_runtime(settings: RadarSettings, sources: SourcesConfig) -> AsyncIterator[Runtime]:
    """Wire production adapters and close every HTTP client and the bot session on exit."""
    from qmemo_radar.interfaces.telegram.bot import (
        TelegramReviewGateway,
        build_bot,
        build_dispatcher,
    )
    from qmemo_radar.interfaces.telegram.controller import TelegramController

    if settings.telegram_bot_token is None or settings.allowed_telegram_id is None:
        raise ValueError("RADAR_TELEGRAM_BOT_TOKEN and RADAR_ALLOWED_TELEGRAM_ID are required")
    quote_publisher, x_publisher = build_publishers(settings)
    application = build_application(settings)
    async with build_x_http_client(settings) as x_http, build_llm_http_client(settings) as llm_http:
        bot = build_bot(settings.telegram_bot_token.get_secret_value())
        try:
            x_client = XApiClient(x_http)
            llm = build_llm_client(settings, llm_http)
            services = build_services(
                application,
                collector=build_x_collector(x_client, settings, sources),
                ranker=LlmRanker(llm),
                writer=LlmDraftWriter(llm),
                gateway=TelegramReviewGateway(
                    bot, chat_id=settings.allowed_telegram_id, timezone=settings.zone
                ),
                lookup=x_client,
                sources=sources,
            )
            controller = TelegramController(
                allowed_user_id=settings.allowed_telegram_id,
                review=services.review,
                drafts=services.drafts,
                runner=services.runner,
                timezone=settings.zone,
            )
            yield Runtime(
                repository=application.repository,
                services=services,
                scheduler=RadarScheduler(
                    runner=services.runner,
                    review=services.review,
                    collect_every=timedelta(minutes=settings.collect_interval_minutes),
                    digest_times=settings.digest_schedule,
                    timezone=settings.zone,
                ),
                controller=controller,
                bot=bot,
                dispatcher=build_dispatcher(controller),
                quote_publisher=quote_publisher,
                x_publisher=x_publisher,
            )
        finally:
            await bot.session.close()


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


def build_llm_client(settings: RadarSettings, http: httpx.AsyncClient) -> ChatCompletionsClient:
    if not settings.llm_model:
        raise ValueError("RADAR_LLM_MODEL is required")
    return ChatCompletionsClient(
        http, model=settings.llm_model, temperature=settings.llm_temperature
    )


def build_llm_http_client(settings: RadarSettings) -> httpx.AsyncClient:
    if not settings.llm_base_url or settings.llm_api_key is None:
        raise ValueError("RADAR_LLM_BASE_URL and RADAR_LLM_API_KEY are required")
    return httpx.AsyncClient(
        base_url=settings.llm_base_url,
        headers={"Authorization": f"Bearer {settings.llm_api_key.get_secret_value()}"},
        timeout=httpx.Timeout(120.0, connect=10.0),
    )


class JsonLogFormatter(logging.Formatter):
    """One JSON object per line with the structured fields; known secrets are redacted."""

    def __init__(self, secrets: Sequence[str] = ()) -> None:
        super().__init__()
        self._secrets = [secret for secret in secrets if secret]

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "module": record.name,
            "message": record.getMessage(),
        }
        for field in _LOG_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        line = json.dumps(entry, ensure_ascii=False, default=str)
        for secret in self._secrets:
            line = line.replace(secret, "[redacted]")
        return line


def configure_logging(settings: RadarSettings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonLogFormatter(settings.secret_values()))
    logging.basicConfig(level=settings.log_level.upper(), handlers=[handler], force=True)
    # These libraries log SQL parameters, request URLs or every update at low levels.
    for noisy in ("aiosqlite", "httpx", "httpcore", "aiogram.event"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
