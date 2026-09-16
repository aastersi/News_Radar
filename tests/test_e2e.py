"""End to end: fake X API -> filtering -> LLM ranking -> SQLite -> Telegram -> draft -> outbox.

Only the network edges are fake (httpx.MockTransport for X and the LLM, FakeGateway and the
controller inbox for Telegram). Everything in between is the production composition.
"""

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from fakes import (
    LLM_CALL_USD,
    OWNER_ID,
    TIMEZONE,
    FakeGateway,
    Inbox,
    open_guard,
    settings_for,
    snowflake,
)

from qmemo_radar.bootstrap import Application, Services, build_services, build_x_collector
from qmemo_radar.config import SourcesConfig
from qmemo_radar.domain import DraftStatus, EventStatus, OutboxStatus
from qmemo_radar.infrastructure.collectors.x_api import XApiClient
from qmemo_radar.infrastructure.drafting import DRAFT_SYSTEM_PROMPT, LlmDraftWriter
from qmemo_radar.infrastructure.llm import ChatCompletionsClient
from qmemo_radar.infrastructure.ranking import RANKING_SYSTEM_PROMPT, LlmRanker
from qmemo_radar.infrastructure.storage import SQLiteEventRepository
from qmemo_radar.interfaces.telegram import render
from qmemo_radar.interfaces.telegram.controller import TelegramController

SOURCES = SourcesConfig.model_validate(
    {
        "x": {
            "accounts": [{"handle": "mara_quinn"}],
            "queries": [{"name": "predictions", "query": "(predicts OR promises) -is:retweet"}],
        },
        "blocked_terms": ["giveaway"],
    }
)
URGENT = 'Mara Quinn: "Every Orbitra wallet will run on solar nodes by 2027. Hold me to that."'
DIGEST = "Orbitra plans to publish a roadmap for wallet nodes next quarter, the team said."


def x_post(post_id: str, text: str, minutes_ago: int = 5) -> dict[str, Any]:
    created = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    return {
        "id": post_id,
        "text": text,
        "author_id": "u1",
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "lang": "en",
        "public_metrics": {"retweet_count": 5, "reply_count": 90, "like_count": 700},
    }


class FakeX:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.posts: dict[str, list[dict[str, Any]]] = {"from:mara_quinn": [], "predicts": []}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        query = request.url.params["query"]
        since = int(request.url.params.get("since_id", "0"))
        key = "from:mara_quinn" if query.startswith("from:") else "predicts"
        posts = [post for post in self.posts[key] if int(post["id"]) > since]
        meta: dict[str, Any] = {"result_count": len(posts)}
        if posts:
            meta["newest_id"] = posts[0]["id"]
        users = [{"id": "u1", "username": "mara_quinn", "name": "Mara Quinn"}]
        return httpx.Response(200, json={"data": posts, "includes": {"users": users}, "meta": meta})


class FakeLlm:
    """Answers ranking and drafting requests the way a well-behaved model would."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.broken = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        payload = json.loads(request.content)
        system, user = payload["messages"][0]["content"], payload["messages"][1]["content"]
        if self.broken:
            return httpx.Response(503)
        if system == RANKING_SYSTEM_PROMPT:
            content = json.dumps({"scores": [_score(post) for post in _block(user, "posts")]})
        else:
            assert system == DRAFT_SYSTEM_PROMPT
            content = json.dumps(_draft(_block(user, "post"), revision="<previous_draft>" in user))
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _block(message: str, name: str) -> Any:
    return json.loads(message.split(f"<{name}>\n", 1)[1].split(f"\n</{name}>", 1)[0])


def _score(post: dict[str, Any]) -> dict[str, Any]:
    urgent = "Hold me to that" in post["text"]
    return {
        "event_id": post["event_id"],
        "qmemo_relevance": 28 if urgent else 20,
        "quote_strength": 19 if urgent else 8,
        "discussion_potential": 14 if urgent else 8,
        "freshness": 15,
        "clarity": 9 if urgent else 8,
        "action_likelihood": 9 if urgent else 6,
        "risk_penalty": 0,
        "headline": "Прогноз Orbitra" if urgent else "Планы Orbitra",
        "summary": "Глава Orbitra дала публичное обещание.",
        "rationale": "Публичное обещание с датой, которое можно проверить позже.",
        "recommended_format": "prediction_tracker",
        "target_action": "follow_prediction",
        "fact_check_required": False,
        "fact_check_note": None,
    }


def _draft(post: dict[str, Any], *, revision: bool) -> dict[str, Any]:
    quote = re.search(r'"([^"]+)"', post["text"])
    return {
        "quote_text": quote.group(1) if quote else post["text"],
        "quote_speaker": "Mara Quinn",
        "context_summary": "Mara Quinn made a dated public promise about Orbitra wallets.",
        "qmemo_text": "Short." if revision else "A dated promise worth checking in 2027.",
        "x_text_template": "Will it happen by 2027? {qmemo_url}",
        "x_text_short": "By 2027?",
        "angle": "Проверка обещания",
        "cta": "Следите за обещанием в QMemo",
        "fact_check_required": False,
        "fact_check_notes": [],
    }


async def _no_sleep(seconds: float) -> None:
    return None


class Radar:
    """A freshly started process: new repository, clients and services on the same database."""

    def __init__(self, db: Path, x: FakeX, llm: FakeLlm) -> None:
        self.repository = SQLiteEventRepository(db)
        self.gateway = FakeGateway()
        # The real SQLite cost ledger: every X and LLM call below is reserved before it is sent.
        guard = open_guard(self.repository)
        x_client = XApiClient(
            httpx.AsyncClient(
                base_url="https://api.x.com", transport=httpx.MockTransport(x.handler)
            ),
            guard=guard,
        )
        chat = ChatCompletionsClient(
            httpx.AsyncClient(
                base_url="https://llm.test/v1", transport=httpx.MockTransport(llm.handler)
            ),
            model="e2e-model",
            guard=guard,
            cost_per_call_usd=LLM_CALL_USD,
            sleep=_no_sleep,
        )
        app = Application(settings=settings_for(self.repository), repository=self.repository)
        self.services: Services = build_services(
            app,
            collector=build_x_collector(x_client, app.settings, SOURCES),
            ranker=LlmRanker(chat),
            writer=LlmDraftWriter(chat),
            gateway=self.gateway,
            lookup=x_client,
            sources=SOURCES,
        )
        self.bot = TelegramController(
            allowed_user_id=OWNER_ID,
            review=self.services.review,
            drafts=self.services.drafts,
            runner=self.services.runner,
            timezone=TIMEZONE,
        )
        self.inbox = Inbox()

    async def start(self) -> "Radar":
        await self.repository.initialize()
        await self.repository.fail_interrupted_runs()
        return self

    def rows(self, query: str) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.repository._db_path) as db:
            return db.execute(query).fetchall()


async def test_full_path_from_x_to_outbox_survives_restart(tmp_path: Path) -> None:
    x, llm = FakeX(), FakeLlm()
    urgent_id, digest_id = snowflake(4, minutes_ago=5), snowflake(3, minutes_ago=6)
    x.posts["from:mara_quinn"] = [x_post(urgent_id, URGENT)]
    x.posts["predicts"] = [
        x_post(digest_id, DIGEST),
        x_post(
            snowflake(2, minutes_ago=7),
            "Giveaway! Follow and repost to win a free Orbitra hoodie today.",
        ),
        x_post(snowflake(1, minutes_ago=300), 'Mara Quinn: "Offices on every continent."', 300),
    ]
    radar = await Radar(tmp_path / "radar.db", x, llm).start()

    # X -> filtering -> ranking -> SQLite -> urgent Telegram card
    await radar.bot.handle_message(OWNER_ID, "/run", radar.inbox)
    assert "SUCCESS" in radar.inbox.last.text
    [(card, urgent, _)] = radar.gateway.cards
    assert urgent and card.event.external_id == urgent_id and card.score.total == 94
    card_text = render.card_text(card, timezone=TIMEZONE, urgent=True)
    assert "Прогноз Orbitra" in card_text and "Балл: <b>94</b>" in card_text
    reasons = sorted(radar.rows("SELECT filter_reason FROM radar_events"), key=str)
    assert reasons == [("blocked_term",), ("too_old",), (None,), (None,)]

    # digest -> second card
    assert await radar.services.review.deliver(urgent=False) == 1
    assert radar.gateway.cards[1][0].event.external_id == digest_id

    # Telegram -> draft -> one revision -> approval -> outbox
    await radar.bot.handle_callback(OWNER_ID, f"e:use:{card.event.event_id}", radar.inbox)
    assert radar.inbox.last.text.startswith("📝 <b>Черновик v1</b>")
    assert "{qmemo_url}" in radar.inbox.last.text
    first = await radar.repository.latest_draft(card.event.event_id)
    assert first is not None and first.quote_author == "Mara Quinn (@mara_quinn)"
    await radar.bot.handle_callback(OWNER_ID, f"d:short:{first.draft_id}", radar.inbox)
    second = await radar.repository.latest_draft(card.event.event_id)
    assert second is not None and second.version == 2 and second.qmemo_text == "Short."
    await radar.bot.handle_callback(OWNER_ID, f"d:ok:{second.draft_id}", radar.inbox)
    assert "Пакет публикации сохранён в outbox" in radar.inbox.last.text

    package = await radar.repository.get_package(card.event.event_id)
    assert package is not None
    assert (
        package.quote_text
        == "Every Orbitra wallet will run on solar nodes by 2027. Hold me to that."
    )
    assert package.source_external_id == urgent_id and package.draft_id == second.draft_id

    # Only read-only X calls and LLM completions happened; nothing was published anywhere.
    assert {(r.method, r.url.host, r.url.path) for r in x.requests} == {
        ("GET", "api.x.com", "/2/tweets/search/recent")
    }
    assert {(r.method, r.url.host, r.url.path) for r in llm.requests} == {
        ("POST", "llm.test", "/v1/chat/completions")
    }

    # Restart: a new process on the same database keeps everything and creates no duplicates.
    restarted = await Radar(tmp_path / "radar.db", x, llm).start()
    x.requests.clear()
    await restarted.bot.handle_message(OWNER_ID, "/run", restarted.inbox)

    since = {r.url.params["query"]: r.url.params.get("since_id") for r in x.requests}
    assert since == {
        "from:mara_quinn -is:retweet": urgent_id,
        "(predicts OR promises) -is:retweet": digest_id,
    }
    assert restarted.gateway.cards == []
    assert restarted.rows("SELECT COUNT(*) FROM radar_events") == [(4,)]
    assert restarted.rows("SELECT version, status FROM drafts ORDER BY version") == [
        (1, DraftStatus.SUPERSEDED.value),
        (2, DraftStatus.ACCEPTED.value),
    ]
    assert restarted.rows("SELECT action FROM feedback ORDER BY id") == [
        ("USE",),
        ("REVISE_SHORTER",),
        ("ACCEPT",),
    ]
    assert await restarted.repository.count_packages(OutboxStatus.APPROVED) == 1
    assert restarted.rows("SELECT COUNT(*) FROM telegram_deliveries") == [(2,)]
    # Every X page and LLM request went through the BudgetGuard ledger before it was sent.
    ledger = dict(restarted.rows("SELECT provider, COUNT(*) FROM cost_ledger GROUP BY provider"))
    assert ledger == {"x": len(x.requests) + 2, "llm.test": len(llm.requests)}
    statuses = await restarted.repository.count_by_status()
    assert statuses[EventStatus.APPROVED.value] == 1 and statuses[EventStatus.NOTIFIED.value] == 1

    await restarted.bot.handle_message(OWNER_ID, "/saved", restarted.inbox)
    assert "outbox): 1" in restarted.inbox.last.text
    await restarted.bot.handle_message(OWNER_ID, "/status", restarted.inbox)
    assert "Outbox APPROVED: 1" in restarted.inbox.last.text
    assert "Публикация в QMemo: выключена · в X: выключена" in restarted.inbox.last.text

    # A stale button from before the restart cannot change the finished decision.
    await restarted.bot.handle_callback(OWNER_ID, f"d:ok:{first.draft_id}", restarted.inbox)
    assert restarted.inbox.last.text == "Материал уже одобрен, пакет в outbox уже есть."
    assert await restarted.repository.count_packages(OutboxStatus.APPROVED) == 1


async def test_unranked_events_are_picked_up_after_restart(tmp_path: Path) -> None:
    x, llm = FakeX(), FakeLlm()
    post_id = snowflake(minutes_ago=5)
    x.posts["from:mara_quinn"] = [x_post(post_id, URGENT)]
    llm.broken = True
    crashed = await Radar(tmp_path / "radar.db", x, llm).start()

    await crashed.bot.handle_message(OWNER_ID, "/run", crashed.inbox)

    assert "PARTIAL" in crashed.inbox.last.text
    assert crashed.gateway.cards == []
    assert await crashed.repository.count_by_status() == {EventStatus.DISCOVERED.value: 1}
    await crashed.repository.start_run("killed-before-finish")  # simulate a hard kill mid-run

    llm.broken = False
    restarted = await Radar(tmp_path / "radar.db", x, llm).start()
    await restarted.bot.handle_message(OWNER_ID, "/run", restarted.inbox)

    assert [card.event.external_id for card, _, _ in restarted.gateway.cards] == [post_id]
    assert restarted.rows(
        "SELECT status, error_summary FROM pipeline_runs WHERE id = 'killed-before-finish'"
    ) == [("FAILED", "interrupted")]
