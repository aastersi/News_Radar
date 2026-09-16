"""Read-only X API v2 adapter: recent search and post lookup. It never writes to X."""

import asyncio
import html
import logging
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
from pydantic import HttpUrl, ValidationError

from qmemo_radar.application.budget import BudgetGuard, PaidFeature
from qmemo_radar.application.filtering import MANUAL_SOURCE_KEY
from qmemo_radar.domain import Engagement, RawSourceItem, SourceFetch, SourceType
from qmemo_radar.exceptions import BudgetBlocked, SourceUnavailable
from qmemo_radar.infrastructure.http import HttpFailure, Sleep, send_with_retry

logger = logging.getLogger(__name__)

X_API_BASE_URL = "https://api.x.com"
# The X guides name the parameter `tweet.fields` (metrics `retweet_count`, long text `note_tweet`)
# while the generated OpenAPI spec says `post.fields`/`repost_count`/`note_post`.
# The guide names are sent; both spellings are accepted when parsing.
_FIELDS = {
    "tweet.fields": "author_id,created_at,lang,note_tweet,public_metrics",
    "expansions": "author_id",
    "user.fields": "name,username",
}
_POST_ID = re.compile(r"[0-9]{1,19}")
_X_EPOCH_MS = 1288834974657  # post ids are snowflakes: (id >> 22) + epoch = creation time in ms
# Recent search only covers 7 days; an older since_id (a quiet account) would be rejected.
SINCE_ID_MAX_AGE = timedelta(days=6)
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")
# Pay-per-use prices from https://docs.x.com/x-api/getting-started/pricing (checked 2026-09-17).
# Whether expanded authors are billed is not documented, so they are counted as user reads.
X_POST_READ_USD = Decimal("0.005")
X_USER_READ_USD = Decimal("0.010")
_PAGE_SIZE = 100


@dataclass(frozen=True, slots=True)
class XQuery:
    source_key: str
    query: str


class XApiClient:
    def __init__(
        self, http: httpx.AsyncClient, *, guard: BudgetGuard, sleep: Sleep = asyncio.sleep
    ) -> None:
        self._http = http
        self._guard = guard
        self._sleep = sleep

    async def search_recent(
        self,
        query: str,
        *,
        source_key: str,
        since_id: str | None,
        start_time: datetime | None,
        max_pages: int,
    ) -> tuple[list[RawSourceItem], str | None]:
        params: dict[str, str | int] = {
            "query": query,
            "max_results": _PAGE_SIZE,
            "sort_order": "recency",
            **_FIELDS,
        }
        # The API accepts at most one of since_id and start_time.
        if since_id:
            params["since_id"] = since_id
        elif start_time:
            params["start_time"] = start_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

        items: list[RawSourceItem] = []
        newest_id: str | None = None
        for page in range(max_pages):
            try:
                body = await self._paid_get(
                    PaidFeature.X_SEARCH, "/2/tweets/search/recent", params, source_key, _PAGE_SIZE
                )
            except BudgetBlocked as exc:
                if page == 0:
                    raise SourceUnavailable(exc.code) from exc
                # Keep the pages already paid for; older posts are skipped as with max_pages.
                logger.warning(
                    "x pagination stopped by budget",
                    extra={
                        "operation": "x_collect",
                        "result": "truncated",
                        "source_key": source_key,
                        "error_code": exc.code,
                    },
                )
                break
            meta = _mapping(body.get("meta"))
            # Results are newest first, so the first page carries the overall newest id.
            newest_id = newest_id or meta.get("newest_id")
            items.extend(_parse_posts(body, source_key))
            next_token = meta.get("next_token")
            if not next_token:
                break
            params["next_token"] = next_token
        else:
            # ponytail: posts past the page limit are skipped; raise max_pages_per_query or
            # narrow the query if this warning repeats.
            logger.warning(
                "x results truncated by max_pages_per_query",
                extra={"operation": "x_collect", "result": "truncated", "source_key": source_key},
            )
        return items, newest_id

    async def lookup_post(self, post_id: str) -> RawSourceItem:
        if not _POST_ID.fullmatch(post_id):
            raise SourceUnavailable("invalid_post_id")
        try:
            body = await self._paid_get(
                PaidFeature.X_LOOKUP, f"/2/tweets/{post_id}", dict(_FIELDS), MANUAL_SOURCE_KEY, 1
            )
        except BudgetBlocked as exc:
            raise SourceUnavailable(exc.code) from exc
        data = body.get("data")
        if not isinstance(data, dict):
            raise SourceUnavailable("not_found")
        items = _parse_posts({"data": [data], "includes": body.get("includes")}, MANUAL_SOURCE_KEY)
        if not items:
            raise SourceUnavailable("invalid_response")
        return items[0]

    async def _paid_get(
        self,
        feature: PaidFeature,
        path: str,
        params: Mapping[str, str | int],
        source_key: str,
        max_posts: int,
    ) -> dict[str, Any]:
        """Reserve the worst case before every attempt; lower it only when the answer is known.

        Raises BudgetBlocked when an attempt is not allowed.
        """
        reserved: list[int] = []

        async def reserve() -> None:
            reserved.append(
                await self._guard.reserve(
                    feature,
                    provider="x",
                    operation=feature.value,
                    units=max_posts * 2,
                    cost_usd=max_posts * (X_POST_READ_USD + X_USER_READ_USD),
                )
            )

        async def answered(response: httpx.Response | None) -> None:
            # X bills the resources it returns and an error status returns none. A lost
            # response (None) may have been served, so its reservation stays.
            if response is not None and response.status_code >= 400:
                await self._guard.settle(reserved[-1], units=0, cost_usd=Decimal(0))

        body = await self._get(path, params, source_key, reserve, answered)
        data = body.get("data")
        posts = len(data) if isinstance(data, list) else int(isinstance(data, dict))
        users = min(len(_sequence(_mapping(body.get("includes")).get("users"))), max_posts)
        await self._guard.settle(
            reserved[-1],
            units=posts + users,
            cost_usd=posts * X_POST_READ_USD + users * X_USER_READ_USD,
        )
        return body

    async def _get(
        self,
        path: str,
        params: Mapping[str, str | int],
        source_key: str,
        before_attempt: Callable[[], Awaitable[None]],
        after_attempt: Callable[[httpx.Response | None], Awaitable[None]],
    ) -> dict[str, Any]:
        try:
            response = await send_with_retry(
                self._http,
                "GET",
                path,
                params=params,
                sleep=self._sleep,
                before_attempt=before_attempt,
                after_attempt=after_attempt,
            )
            body = response.json()
        except HttpFailure as exc:
            suffix = f"_{exc.status_code}" if exc.status_code else ""
            raise SourceUnavailable(f"{exc.code}{suffix}") from exc
        except ValueError as exc:
            raise SourceUnavailable("invalid_response") from exc
        if not isinstance(body, dict):
            raise SourceUnavailable("invalid_response")
        if body.get("errors"):
            logger.warning(
                "x api partial response",
                extra={
                    "operation": "x_request",
                    "result": "partial",
                    "source_key": source_key,
                    "error_code": f"errors={len(body['errors'])}",
                },
            )
        return body


class XRecentSearchCollector:
    def __init__(
        self,
        client: XApiClient,
        queries: Sequence[XQuery],
        *,
        lookback: timedelta,
        max_pages: int = 3,
    ) -> None:
        self._client = client
        self._queries = list(queries)
        self._lookback = lookback
        self._max_pages = max_pages

    async def collect(self, checkpoints: Mapping[str, str]) -> list[SourceFetch]:
        first_run_start = datetime.now(UTC) - self._lookback
        fetches: list[SourceFetch] = []
        for query in self._queries:
            since_id = checkpoints.get(query.source_key)
            if since_id and not _is_recent_post_id(since_id, first_run_start):
                since_id = None  # fall back to start_time for sources that were quiet for days
            try:
                items, newest_id = await self._client.search_recent(
                    query.query,
                    source_key=query.source_key,
                    since_id=since_id,
                    start_time=None if since_id else first_run_start,
                    max_pages=self._max_pages,
                )
            except SourceUnavailable as exc:
                logger.warning(
                    "x source failed",
                    extra={
                        "operation": "x_collect",
                        "result": "failed",
                        "source_key": query.source_key,
                        "error_code": exc.code,
                    },
                )
                fetches.append(SourceFetch(source_key=query.source_key, error_code=exc.code))
                continue
            fetches.append(
                SourceFetch(
                    source_key=query.source_key,
                    items=tuple(items),
                    cursor=newest_id or since_id,
                )
            )
        return fetches


def _mapping(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _sequence(value: object) -> list[Any]:
    return value if isinstance(value, list) else []


def _is_recent_post_id(post_id: str, reference: datetime) -> bool:
    if not _POST_ID.fullmatch(post_id):
        return False
    created = datetime.fromtimestamp(((int(post_id) >> 22) + _X_EPOCH_MS) / 1000, UTC)
    return created > reference - SINCE_ID_MAX_AGE


def _parse_posts(body: Mapping[str, Any], source_key: str) -> list[RawSourceItem]:
    includes = _mapping(body.get("includes"))
    users = {
        user["id"]: user
        for user in _sequence(includes.get("users"))
        if isinstance(user, dict) and "id" in user
    }
    posts = _sequence(body.get("data"))
    items: list[RawSourceItem] = []
    for post in posts:
        try:
            items.append(_parse_post(post, users, source_key))
        except (AttributeError, KeyError, TypeError, ValueError, ValidationError):
            logger.warning(
                "skipped malformed x post",
                extra={"operation": "x_parse", "result": "skipped", "source_key": source_key},
            )
    return items


def _parse_post(
    post: Mapping[str, Any],
    users: Mapping[str, Mapping[str, Any]],
    source_key: str,
) -> RawSourceItem:
    post_id = str(post["id"])
    author = users.get(post.get("author_id") or "", {})
    username = author.get("username")
    handle = username if isinstance(username, str) and _HANDLE.fullmatch(username) else None
    note = post.get("note_tweet") or post.get("note_post") or {}
    # X returns text with HTML entities (&amp;); the stored original must match what people read.
    text = html.unescape(note.get("text") or post["text"])
    metrics = post.get("public_metrics") or {}
    url = (
        f"https://x.com/{handle}/status/{post_id}"
        if handle
        else f"https://x.com/i/web/status/{post_id}"
    )
    return RawSourceItem(
        source=SourceType.X,
        external_id=post_id,
        url=HttpUrl(url),
        author_id=post.get("author_id"),
        author_handle=handle,
        author_display_name=author.get("name"),
        original_text=text,
        language=post.get("lang"),
        published_at=datetime.fromisoformat(post["created_at"]),
        engagement=Engagement(
            likes=metrics.get("like_count", 0),
            reposts=metrics.get("retweet_count", metrics.get("repost_count", 0)),
            replies=metrics.get("reply_count", 0),
            quotes=metrics.get("quote_count", 0),
            views=metrics.get("impression_count"),
        ),
        raw_payload={"id": post_id, "author_id": post.get("author_id"), "lang": post.get("lang")},
        source_key=source_key,
    )
