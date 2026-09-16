from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from qmemo_radar.domain import EventCandidate


@dataclass(frozen=True, slots=True)
class FilterPolicy:
    max_age: timedelta
    minimum_text_length: int = 20
    blocked_authors: frozenset[str] = frozenset()
    blocked_terms: frozenset[str] = frozenset()


def first_filter_reason(
    event: EventCandidate,
    policy: FilterPolicy,
    *,
    now: datetime | None = None,
) -> str | None:
    current = now or datetime.now(UTC)
    published_at = event.published_at
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)

    if published_at < current - policy.max_age:
        return "too_old"
    if event.author_handle and event.author_handle.casefold() in policy.blocked_authors:
        return "blocked_author"
    if len(event.normalized_text) < policy.minimum_text_length:
        return "too_short"
    if any(term in event.normalized_text for term in policy.blocked_terms):
        return "blocked_term"
    return None

