import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import HttpUrl

from qmemo_radar.domain import EventCandidate, RawSourceItem

_WHITESPACE = re.compile(r"\s+")
_TRACKING_PARAMETERS = {
    "fbclid",
    "gclid",
    "ref",
    "source",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}


def normalize_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value)
    return _WHITESPACE.sub(" ", normalized).strip()


def canonicalize_url(value: str) -> str:
    parts = urlsplit(value)
    host = parts.hostname.lower() if parts.hostname else ""
    if host == "twitter.com" or host == "www.twitter.com":
        host = "x.com"
    if parts.port:
        host = f"{host}:{parts.port}"
    query = urlencode(
        [
            (key, item)
            for key, item in parse_qsl(parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMETERS
        ]
    )
    return urlunsplit((parts.scheme.lower(), host, parts.path.rstrip("/"), query, ""))


def comparison_text(value: str) -> str:
    return normalize_text(value).casefold()


def build_candidate(
    item: RawSourceItem,
    *,
    discovered_at: datetime | None = None,
) -> EventCandidate:
    normalized = comparison_text(item.original_text)
    content_hash = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return EventCandidate(
        **item.model_dump(exclude={"url"}),
        url=HttpUrl(canonicalize_url(str(item.url))),
        discovered_at=discovered_at or datetime.now(UTC),
        normalized_text=normalized,
        content_hash=content_hash,
    )
