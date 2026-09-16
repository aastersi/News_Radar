"""BudgetGuard and the cost ledger: paid calls fail closed before any HTTP request."""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest
from fakes import snowflake, x_item

from qmemo_radar.application.budget import BudgetGuard, PaidFeature
from qmemo_radar.application.normalization import build_candidate
from qmemo_radar.bootstrap import build_budget_guard
from qmemo_radar.config import RadarSettings
from qmemo_radar.domain import CostEntry
from qmemo_radar.exceptions import BudgetBlocked, RankingFailed, SourceUnavailable
from qmemo_radar.infrastructure.collectors.x_api import XApiClient
from qmemo_radar.infrastructure.llm import ChatCompletionsClient
from qmemo_radar.infrastructure.ranking import LlmRanker
from qmemo_radar.infrastructure.storage import SQLiteEventRepository

SINCE = datetime(2000, 1, 1, tzinfo=UTC)
CHAT = [{"role": "user", "content": "hi"}]


class Wire:
    """Counts requests that actually reached the transport."""

    def __init__(self, body: dict[str, Any] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self._body = body or {}

    def http(self, base_url: str) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(self.handler))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json=self._body)


def settings(**values: Any) -> RadarSettings:
    return RadarSettings(_env_file=None, **values)


async def search(x: XApiClient) -> None:
    await x.search_recent("q", source_key="query:q", since_id=None, start_time=None, max_pages=1)


def test_defaults_are_free_and_capped() -> None:
    defaults = settings()
    assert defaults.cost_target_usd_monthly == 0
    assert defaults.cost_hard_limit_usd_monthly == 10
    assert not defaults.paid_sources_enabled
    assert not defaults.paid_llm_enabled
    assert not defaults.x_paid_search_enabled
    assert defaults.llm_cost_per_call_usd is None
    with pytest.raises(ValueError):
        settings(cost_hard_limit_usd_monthly="10.01")
    with pytest.raises(ValueError):
        settings(cost_target_usd_monthly="5", cost_hard_limit_usd_monthly="2")
    # No paid feature is enabled, so no paid credential is required.
    problems = settings(telegram_bot_token="t", allowed_telegram_id=1).production_problems()
    assert not [problem for problem in problems if "X_BEARER" in problem or "LLM" in problem]


def test_enabled_paid_features_require_their_credentials_and_price() -> None:
    problems = settings(
        paid_sources_enabled=True, x_paid_search_enabled=True, paid_llm_enabled=True
    ).production_problems()
    for name in ("RADAR_X_BEARER_TOKEN", "RADAR_LLM_API_KEY", "RADAR_LLM_COST_PER_CALL_USD"):
        assert f"{name} is required" in problems
    lonely_flag = settings(x_paid_search_enabled=True).production_problems()
    assert any("requires RADAR_PAID_SOURCES_ENABLED" in problem for problem in lonely_flag)


async def test_default_configuration_blocks_every_paid_call_before_http(
    repository: SQLiteEventRepository,
) -> None:
    guard = build_budget_guard(settings(llm_cost_per_call_usd="0.01"), repository)
    x_wire, llm_wire = Wire(), Wire()
    x = XApiClient(x_wire.http("https://api.x.com"), guard=guard)
    llm = ChatCompletionsClient(
        llm_wire.http("https://llm.test/v1"), model="m", guard=guard, cost_per_call_usd=Decimal(1)
    )

    with pytest.raises(SourceUnavailable, match="paid_disabled"):
        await x.lookup_post("20")
    with pytest.raises(SourceUnavailable, match="paid_disabled"):
        await search(x)
    with pytest.raises(BudgetBlocked, match="paid_disabled"):
        await llm.complete(CHAT, max_tokens=10)
    with pytest.raises(RankingFailed) as ranking:
        await LlmRanker(llm).rank([build_candidate(x_item(1))])

    # The ranker treats a blocked budget like an unreachable provider: retry later, never drop.
    assert ranking.value.code == "paid_disabled" and ranking.value.retryable
    assert x_wire.requests == [] and llm_wire.requests == []
    assert await repository.cost_since(SINCE) == 0


async def test_unknown_llm_price_is_blocked_even_when_paid_llm_is_enabled(
    repository: SQLiteEventRepository,
) -> None:
    guard = build_budget_guard(settings(paid_llm_enabled=True), repository)
    wire = Wire()
    llm = ChatCompletionsClient(
        wire.http("https://llm.test/v1"), model="m", guard=guard, cost_per_call_usd=None
    )

    with pytest.raises(BudgetBlocked, match="unknown_cost"):
        await llm.complete(CHAT, max_tokens=10)
    assert wire.requests == []


async def test_hard_limit_blocks_before_http_and_x_is_charged_for_what_it_read(
    repository: SQLiteEventRepository,
) -> None:
    guard = BudgetGuard(
        repository,
        enabled=frozenset(PaidFeature),
        hard_limit_usd=Decimal("1.52"),
        target_usd=Decimal(0),
    )
    post_id = snowflake(minutes_ago=1)
    wire = Wire(
        {
            "data": [
                {
                    "id": post_id,
                    "text": "A founder promised a public launch date this week.",
                    "author_id": "u1",
                    "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                }
            ],
            "includes": {"users": [{"id": "u1", "username": "founder", "name": "F"}]},
            "meta": {"newest_id": post_id},
        }
    )
    x = XApiClient(wire.http("https://api.x.com"), guard=guard)

    # A page reserves 100 posts + 100 users ($1.50) and is settled to 1 post + 1 user.
    await search(x)
    assert await repository.cost_since(SINCE) == Decimal("0.015")
    await search(x)
    assert await repository.cost_since(SINCE) == Decimal("0.03")

    # 0.03 spent + a $1.50 worst-case page would cross $1.52: refused before sending.
    with pytest.raises(SourceUnavailable, match="hard_limit_reached"):
        await search(x)
    assert len(wire.requests) == 2

    # A single lookup still fits and is sent (this fake answers with a search-shaped body);
    # once the month is full it is refused before sending too.
    with pytest.raises(SourceUnavailable, match="not_found"):
        await x.lookup_post(post_id)
    remaining = Decimal("1.52") - await repository.cost_since(SINCE)
    fill = CostEntry(
        provider="test",
        operation="fill",
        units=1,
        estimated_cost_usd=remaining,
        created_at=datetime.now(UTC),
    )
    assert await repository.reserve_cost(fill, since=SINCE, limit_usd=Decimal("1.52"))
    with pytest.raises(SourceUnavailable, match="hard_limit_reached"):
        await x.lookup_post(post_id)
    assert len(wire.requests) == 3
    assert await repository.cost_since(SINCE) == Decimal("1.52")


async def test_concurrent_reservations_cannot_overspend(
    repository: SQLiteEventRepository,
) -> None:
    guard = BudgetGuard(
        repository,
        enabled=frozenset({PaidFeature.LLM}),
        hard_limit_usd=Decimal(1),
        target_usd=Decimal(0),
    )

    async def call() -> bool:
        try:
            await guard.reserve(
                PaidFeature.LLM, provider="llm", operation="chat", units=1, cost_usd=Decimal("0.3")
            )
        except BudgetBlocked:
            return False
        return True

    results = await asyncio.gather(*(call() for _ in range(20)))

    assert results.count(True) == 3
    assert await guard.spent_this_month() == Decimal("0.9")

