"""Read-only X API v2 adapter: recent search and post lookup. It never writes to X."""

import asyncio
import html
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import HttpUrl, ValidationError

from qmemo_radar.application.filtering import MANUAL_SOURCE_KEY
from qmemo_radar.domain import Engagement, RawSourceItem, SourceFetch, SourceType
from qmemo_radar.exceptions import SourceUnavailable
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
_HANDLE = re.compile(r"[A-Za-z0-9_]{1,15}")


@dataclass(frozen=True, slots=True)
class XQuery:
    source_key: str
    query: str


class XApiClient:
    def __init__(self, http: httpx.AsyncClient, *, sleep: Sleep = asyncio.sleep) -> None:
        self._http = http
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
            "max_results": 100,
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
        for _ in range(max_pages):
            body = await self._get("/2/tweets/search/recent", params, source_key)
            meta = body.get("meta") or {}
            # Results are newest first, so the first page carries the overall newest id.
            newest_id = newest_id or meta.get("newest_id")
            items.extend(_parse_posts(body, source_key))
            next_token = meta.get("next_token")
            if not next_token:
                break
            params["next_token"] = next_token
        return items, newest_id

    async def lookup_post(self, post_id: str) -> RawSourceItem:
        if not _POST_ID.fullmatch(post_id):
            raise SourceUnavailable("invalid_post_id")
        body = await self._get(f"/2/tweets/{post_id}", dict(_FIELDS), MANUAL_SOURCE_KEY)
        data = body.get("data")
        if not isinstance(data, dict):
            raise SourceUnavailable("not_found")
        items = _parse_posts({"data": [data], "includes": body.get("includes")}, MANUAL_SOURCE_KEY)
        if not items:
            raise SourceUnavailable("invalid_response")
        return items[0]

    async def _get(
        self,
        path: str,
        params: Mapping[str, str | int],
        source_key: str,
    ) -> dict[str, Any]:
        try:
            response = await send_with_retry(
                self._http, "GET", path, params=params, sleep=self._sleep
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


def _parse_posts(body: Mapping[str, Any], source_key: str) -> list[RawSourceItem]:
    includes = body.get("includes") or {}
    users = {
        user["id"]: user
        for user in includes.get("users") or []
        if isinstance(user, dict) and "id" in user
    }
    items: list[RawSourceItem] = []
    for post in body.get("data") or []:
        try:
            items.append(_parse_post(post, users, source_key))
        except (KeyError, TypeError, ValueError, ValidationError):
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
