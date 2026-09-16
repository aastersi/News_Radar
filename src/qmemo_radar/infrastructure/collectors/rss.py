"""RSS 2.0 and Atom feeds from sources.yaml, parsed with the standard library only.

Each feed has its own source key `rss:<name>` and cursor: a JSON object with the `etag` and
`last_modified` of the last 200 response, sent back as If-None-Match / If-Modified-Since.
304 is a successful poll without items. A feed that fails (network, status, size, XML, unsafe
address) reports only its own error; the others continue.

No JavaScript, no page scraping: only the feed document itself is read. Redirects are followed
by hand, at most MAX_REDIRECTS hops, and every hop must resolve to public addresses only.
"""

import asyncio
import hashlib
import html
import ipaddress
import json
import logging
import re
import socket
import xml.etree.ElementTree as ElementTree
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import HttpUrl

from qmemo_radar.application.normalization import canonicalize_url, normalize_text
from qmemo_radar.domain import RawSourceItem, SourceFetch, SourceType
from qmemo_radar.infrastructure.http import HttpFailure, Sleep, get_limited

logger = logging.getLogger(__name__)

MAX_REDIRECTS = 5
MAX_CONCURRENT_FEEDS = 8
MAX_SUMMARY_CHARS = 2_000
_REDIRECTS = {301, 302, 303, 307, 308}
_TAGS = re.compile(r"<[^>]*>")
_XML_LANG = "{http://www.w3.org/XML/1998/namespace}lang"

Resolver = Callable[[str, int], Awaitable[list[str]]]


@dataclass(frozen=True, slots=True)
class Feed:
    name: str
    url: str

    @property
    def source_key(self) -> str:
        return f"rss:{self.name}"


class UnsafeAddress(Exception):
    """The feed URL is not http(s) or resolves to a private, loopback or reserved address."""


class RssCollector:
    def __init__(
        self,
        http: httpx.AsyncClient,
        feeds: Sequence[Feed],
        *,
        max_bytes: int,
        resolve: Resolver | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._http = http
        self._feeds = tuple(feeds)
        self._max_bytes = max_bytes
        self._resolve = resolve or _resolve
        self._clock = clock
        self._sleep = sleep
        self._slots = asyncio.Semaphore(MAX_CONCURRENT_FEEDS)

    async def collect(self, checkpoints: Mapping[str, str]) -> list[SourceFetch]:
        return list(
            await asyncio.gather(
                *(self._poll(feed, checkpoints.get(feed.source_key)) for feed in self._feeds)
            )
        )

    async def _poll(self, feed: Feed, cursor: str | None) -> SourceFetch:
        stats: Counter[str] = Counter(feeds_checked=1)
        try:
            async with self._slots:
                response, body, final_url = await self._get(feed.url, _conditional(cursor))
            if response.status_code == 304:
                stats["feeds_304"] += 1
                return SourceFetch(source_key=feed.source_key, cursor=cursor, stats=stats)
            if response.status_code != 200:
                raise HttpFailure("http_status", response.status_code)
            items = await asyncio.to_thread(self._parse, feed, body, final_url, stats)
        except Exception as exc:  # one broken feed never stops the others
            code = _error_code(exc)
            stats["feed_errors"] += 1
            logger.warning(
                "rss feed failed",
                extra={"operation": "collect", "source_key": feed.source_key, "error_code": code},
            )
            return SourceFetch(source_key=feed.source_key, error_code=code, stats=stats)
        state = {
            "etag": response.headers.get("etag"),
            "last_modified": response.headers.get("last-modified"),
        }
        return SourceFetch(
            source_key=feed.source_key,
            items=tuple(items),
            # Always a JSON object: validators the server stopped sending are cleared.
            cursor=json.dumps({key: value for key, value in state.items() if value}),
            stats=stats,
        )

    async def _get(
        self, url: str, headers: dict[str, str]
    ) -> tuple[httpx.Response, bytes, str]:
        for _ in range(MAX_REDIRECTS + 1):
            await self._check_public(url)
            response, body = await get_limited(
                self._http, url, max_bytes=self._max_bytes, headers=headers, sleep=self._sleep
            )
            location = response.headers.get("location")
            if response.status_code not in _REDIRECTS or not location:
                return response, body, url
            url = urljoin(url, location)
        raise HttpFailure("too_many_redirects")

    async def _check_public(self, url: str) -> None:
        # ponytail: checked before connecting, so DNS rebinding between the check and the
        # request is possible; pin resolved addresses in a custom transport if feeds become
        # untrusted input.
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise UnsafeAddress(url)
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
            addresses = await self._resolve(parts.hostname, port)
        except (OSError, ValueError) as exc:  # DNS failure, or an invalid port in the URL
            raise HttpFailure("network_error") from exc
        try:
            public = bool(addresses) and all(ipaddress.ip_address(a).is_global for a in addresses)
        except ValueError:  # e.g. a scoped IPv6 link-local address
            public = False
        if not public:
            raise UnsafeAddress(url)

    def _parse(
        self, feed: Feed, body: bytes, base_url: str, stats: Counter[str]
    ) -> list[RawSourceItem]:
        if b"<!ENTITY" in body:
            # Feeds never need entity declarations; refusing them avoids expansion attacks.
            raise ElementTree.ParseError("entity declarations are not accepted")
        root = ElementTree.fromstring(body)
        if _local(root.tag) not in ("rss", "feed", "rdf"):
            raise ElementTree.ParseError(f"not a feed: {_local(root.tag)}")
        channel_language = _text(_child(_child(root, "channel"), "language")) or root.get(_XML_LANG)
        now = self._clock()
        items: list[RawSourceItem] = []
        for entry in root.iter():
            if _local(entry.tag) not in ("item", "entry"):
                continue
            stats["entries_seen"] += 1
            try:
                item = self._entry(feed, entry, base_url, channel_language, now)
            except ValueError:  # includes pydantic's ValidationError and malformed URLs
                item = None
            if item is None:
                stats["entries_rejected"] += 1
            else:
                items.append(item)
        stats["entries_accepted"] += len(items)
        return items

    def _entry(  # raises ValueError for an entry that cannot become an item
        self,
        feed: Feed,
        entry: ElementTree.Element,
        base_url: str,
        feed_language: str | None,
        now: datetime,
    ) -> RawSourceItem | None:
        link = _link(entry)
        guid = _text(_child(entry, "guid")) or _text(_child(entry, "id"))
        url = urljoin(base_url, link) if link else guid if guid.startswith("http") else ""
        title = _plain(_text(_child(entry, "title")))
        summary = _plain(
            _text(_child(entry, "description"))
            or _text(_child(entry, "summary"))
            or _text(_child(entry, "content"))
        )[:MAX_SUMMARY_CHARS]
        text = "\n\n".join(part for part in (title, summary) if part)
        if not url or not text:
            return None
        identity = f"{feed.name}:{guid or canonicalize_url(url)}"
        if len(identity) > 255:
            identity = f"{feed.name}:sha256:{hashlib.sha256(identity.encode()).hexdigest()}"
        author = _plain(
            _text(_child(_child(entry, "author"), "name"))
            or _text(_child(entry, "author"))
            or _text(_child(entry, "creator"))
        )
        return RawSourceItem(
            source=SourceType.RSS,
            external_id=identity,
            url=HttpUrl(url),
            author_display_name=author[:200] or None,
            original_text=text,
            language=entry.get(_XML_LANG) or feed_language or None,
            published_at=_published(entry) or now,
            raw_payload={
                "feed": feed.name,
                "title": title,
                "summary": summary,
                "guid": guid or None,
                "link": url,
            },
            source_key=feed.source_key,
        )


def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _child(element: ElementTree.Element | None, name: str) -> ElementTree.Element | None:
    if element is None:
        return None
    return next((child for child in element if _local(child.tag) == name), None)


def _text(element: ElementTree.Element | None) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def _link(entry: ElementTree.Element) -> str:
    for child in entry:
        if _local(child.tag) != "link":
            continue
        if child.get("href"):  # Atom
            if child.get("rel", "alternate") == "alternate":
                return child.get("href", "").strip()
        elif _text(child):  # RSS
            return _text(child)
    return ""


def _plain(value: str) -> str:
    """Feed text without markup: tags removed, entities decoded, whitespace collapsed."""
    return normalize_text(html.unescape(_TAGS.sub(" ", html.unescape(value))))


def _published(entry: ElementTree.Element) -> datetime | None:
    for name in ("pubdate", "published", "updated", "date"):
        value = _text(_child(entry, name))
        if not value:
            continue
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            try:
                moment = parsedate_to_datetime(value)
            except (TypeError, ValueError):
                continue
        return moment if moment.tzinfo else moment.replace(tzinfo=UTC)
    return None


def _conditional(cursor: str | None) -> dict[str, str]:
    try:
        state = json.loads(cursor) if cursor else {}
    except ValueError:
        state = {}
    headers: dict[str, str] = {}
    if isinstance(state, dict):
        if isinstance(state.get("etag"), str):
            headers["If-None-Match"] = state["etag"]
        if isinstance(state.get("last_modified"), str):
            headers["If-Modified-Since"] = state["last_modified"]
    return headers


def _error_code(exc: Exception) -> str:
    if isinstance(exc, UnsafeAddress):
        return "rss_unsafe_address"
    if isinstance(exc, ElementTree.ParseError):
        return "rss_malformed_xml"
    if not isinstance(exc, HttpFailure):
        return f"rss_failed:{type(exc).__name__}"
    if exc.code == "http_status":
        return f"rss_http_{exc.status_code}"
    return f"rss_{exc.code}"


async def _resolve(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]
