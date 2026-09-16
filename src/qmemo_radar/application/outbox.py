import hashlib
from datetime import datetime

from qmemo_radar.domain import (
    Draft,
    DraftStatus,
    EventCandidate,
    EventStatus,
    PublicationPackage,
)


def idempotency_key(event: EventCandidate) -> str:
    """Stable per source post: one post can never produce a second package."""
    raw = f"qmemo-radar:package:v1:{event.source.value}:{event.external_id}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_publication_package(
    event: EventCandidate,
    draft: Draft,
    *,
    approved_by: int,
    approved_at: datetime,
) -> PublicationPackage:
    """Raises ValueError when the event or draft is not in an approvable state."""
    if draft.event_id != event.event_id:
        raise ValueError("draft belongs to another event")
    if event.status is not EventStatus.DRAFTED:
        raise ValueError("only a DRAFTED event can be approved")
    if draft.status is not DraftStatus.ACTIVE:
        raise ValueError("only the active draft version can be approved")
    return PublicationPackage(
        event_id=event.event_id,
        draft_id=draft.draft_id,
        quote_text=draft.quote_text,
        quote_author=draft.quote_author,
        quote_language=draft.quote_language,
        context_summary=draft.context_summary,
        qmemo_text=draft.qmemo_text,
        source_type=event.source,
        source_external_id=event.external_id,
        source_url=event.url,
        source_published_at=event.published_at,
        x_text_template=draft.x_text_template,
        x_text_short=draft.x_text_short,
        cta=draft.cta,
        fact_check_status=draft.fact_check_status,
        fact_check_notes=draft.fact_check_notes,
        approved_by_telegram_id=approved_by,
        approved_at=approved_at,
        idempotency_key=idempotency_key(event),
    )
