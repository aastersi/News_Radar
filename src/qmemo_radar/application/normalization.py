import hashlib
import re
import unicodedata
from datetime import UTC, datetime
from urllib.parse import unquote_plus, urlsplit, urlunsplit

from pydantic import HttpUrl

from qmemo_radar.domain import EventCandidate, RawSourceItem

_WHITESPACE = re.compile(r"\s+")
_X_STATUS_URL = re.compile(
    r"https://x\.com/(?P<handle>[A-Za-z0-9_]{1,15})/status/(?P<post_id>[0-9]{1,19})/?(?:\?[^\s#]*)?"
)
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
    if ":" in host:
        host = f"[{host}]"  # IPv6 literal
    if parts.port:
        host = f"{host}:{parts.port}"
    # Parameters keep their original encoding: re-encoding could lengthen or break a valid URL.
    query = "&".join(
        pair
        for pair in parts.query.split("&")
        if pair and unquote_plus(pair.split("=", 1)[0]).lower() not in _TRACKING_PARAMETERS
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


def parse_x_status_url(value: str) -> str | None:
    """Return the post id only for https://x.com/<handle>/status/<id>; anything else is rejected."""
    match = _X_STATUS_URL.fullmatch(value.strip())
    return match.group("post_id") if match else None
