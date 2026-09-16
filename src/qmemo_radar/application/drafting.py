import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from qmemo_radar.application.outbox import build_publication_package
from qmemo_radar.application.ports import DraftWriter, OutboxRepository
from qmemo_radar.domain import (
    Draft,
    DraftStatus,
    DraftText,
    EventCandidate,
    EventStatus,
    FactCheckStatus,
    FeedbackAction,
    OutboxStatus,
    PublicationPackage,
    ScoredEvent,
)
from qmemo_radar.exceptions import DraftFailed

logger = logging.getLogger(__name__)

MAX_DRAFT_VERSIONS = 2
_QUOTES = str.maketrans({c: '"' for c in "“”„‟«»″"} | {c: "'" for c in "‘’‚‛′"})
_SPACES = re.compile(r"\s+")
_TRIM = " \"'"


class RevisionMode(StrEnum):
    SHORTER = "shorter"
    ANGLE = "angle"
    CUSTOM = "custom"


REVISION_INSTRUCTIONS = {
    RevisionMode.SHORTER: "Make every text noticeably shorter. Keep the same exact quote.",
    RevisionMode.ANGLE: "Choose a clearly different marketing angle. The quote may stay the same.",
}


class DraftOutcome(StrEnum):
    CREATED = "CREATED"
    EXISTS = "EXISTS"
    IN_PROGRESS = "IN_PROGRESS"
    NOT_FOUND = "NOT_FOUND"
    CLOSED = "CLOSED"
    STALE = "STALE"
    LIMIT_REACHED = "LIMIT_REACHED"
    FAILED = "FAILED"
    NEEDS_VERIFICATION = "NEEDS_VERIFICATION"
    APPROVED = "APPROVED"
    ALREADY_APPROVED = "ALREADY_APPROVED"


@dataclass(frozen=True, slots=True)
class DraftResult:
    outcome: DraftOutcome
    draft: Draft | None = None
    package: PublicationPackage | None = None


def normalize_for_quote(value: str) -> str:
    """Safe normalization only: Unicode form, quote characters and whitespace."""
    normalized = unicodedata.normalize("NFKC", value).translate(_QUOTES)
    return _SPACES.sub(" ", normalized).strip()


def quote_in_source(quote: str, source: str) -> bool:
    needle = normalize_for_quote(quote).strip(_TRIM)
    return len(needle) >= 3 and needle in normalize_for_quote(source)


class DraftService:
    """Creates, revises and verifies drafts. One first version and one revision per event."""

    def __init__(
        self,
        *,
        repository: OutboxRepository,
        writer: DraftWriter,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._repository = repository
        self._writer = writer
        self._clock = clock
        # ponytail: in-process guard against double presses; the DB constraints still hold
        # if a second process ever appears.
        self._busy: set[str] = set()

    async def use(self, event_id: str, user_id: int) -> DraftResult:
        if event_id in self._busy:
            return DraftResult(DraftOutcome.IN_PROGRESS)
        self._busy.add(event_id)
        try:
            card = await self._repository.get_scored_event(event_id)
            if card is None:
                return DraftResult(DraftOutcome.NOT_FOUND)
            existing = await self._repository.latest_draft(event_id)
            if existing is not None:
                return DraftResult(DraftOutcome.EXISTS, existing)
            if card.event.status not in (EventStatus.NOTIFIED, EventStatus.SNOOZED):
                return DraftResult(DraftOutcome.CLOSED)
            draft = await self._write(card, version=1)
            if draft is None:
                return DraftResult(DraftOutcome.FAILED)
            if not await self._repository.save_first_draft(draft, telegram_user_id=user_id):
                return DraftResult(DraftOutcome.CLOSED)
            return DraftResult(DraftOutcome.CREATED, draft)
        finally:
            self._busy.discard(event_id)

    async def revise(
        self,
        draft_id: str,
        user_id: int,
        mode: RevisionMode,
        instruction: str | None = None,
    ) -> DraftResult:
        previous = await self._repository.get_draft(draft_id)
        if previous is None:
            return DraftResult(DraftOutcome.NOT_FOUND)
        if previous.event_id in self._busy:
            return DraftResult(DraftOutcome.IN_PROGRESS)
        self._busy.add(previous.event_id)
        try:
            stale = await self._stale(previous)
            if stale:
                return stale
            if previous.version >= MAX_DRAFT_VERSIONS:
                return DraftResult(DraftOutcome.LIMIT_REACHED, previous)
            card = await self._repository.get_scored_event(previous.event_id)
            if card is None or card.event.status is not EventStatus.DRAFTED:
                return DraftResult(DraftOutcome.CLOSED)
            text = REVISION_INSTRUCTIONS.get(mode, instruction)
            draft = await self._write(card, version=2, previous=previous, instruction=text)
            if draft is None:
                return DraftResult(DraftOutcome.FAILED, previous)
            action = {
                RevisionMode.SHORTER: FeedbackAction.REVISE_SHORTER,
                RevisionMode.ANGLE: FeedbackAction.REVISE_ANGLE,
                RevisionMode.CUSTOM: FeedbackAction.REVISE_CUSTOM,
            }[mode]
            saved = await self._repository.save_revision(
                draft, previous_id=previous.draft_id, action=action, telegram_user_id=user_id
            )
            if not saved:
                return DraftResult(DraftOutcome.LIMIT_REACHED, previous)
            return DraftResult(DraftOutcome.CREATED, draft)
        finally:
            self._busy.discard(previous.event_id)

    async def revisable_draft(self) -> Draft | None:
        """The draft a free-text message may revise: newest active first version, if any."""
        return await self._repository.latest_revisable_draft()

    async def verify(self, draft_id: str, user_id: int) -> DraftResult:
        draft = await self._repository.get_draft(draft_id)
        if draft is None:
            return DraftResult(DraftOutcome.NOT_FOUND)
        stale = await self._stale(draft)
        if stale:
            return stale
        if draft.fact_check_status is FactCheckStatus.VERIFIED:
            return DraftResult(DraftOutcome.EXISTS, draft)
        if not await self._repository.mark_verified(draft_id, telegram_user_id=user_id):
            return DraftResult(DraftOutcome.STALE)
        return DraftResult(
            DraftOutcome.CREATED,
            draft.model_copy(update={"fact_check_status": FactCheckStatus.VERIFIED}),
        )

    async def reject(self, draft_id: str, user_id: int) -> DraftResult:
        draft = await self._repository.get_draft(draft_id)
        if draft is None:
            return DraftResult(DraftOutcome.NOT_FOUND)
        stale = await self._stale(draft)
        if stale:
            return stale
        if not await self._repository.reject_draft(draft, telegram_user_id=user_id):
            return DraftResult(DraftOutcome.STALE)
        return DraftResult(DraftOutcome.CREATED, draft)

    async def accept(self, draft_id: str, user_id: int) -> DraftResult:
        """Approve the latest verified draft into exactly one outbox package. Never publishes."""
        draft = await self._repository.get_draft(draft_id)
        if draft is None:
            return DraftResult(DraftOutcome.NOT_FOUND)
        existing = await self._repository.get_package(draft.event_id)
        if existing is not None:
            return DraftResult(DraftOutcome.ALREADY_APPROVED, draft, existing)
        stale = await self._stale(draft)
        if stale:
            return stale
        if draft.fact_check_status is not FactCheckStatus.VERIFIED:
            return DraftResult(DraftOutcome.NEEDS_VERIFICATION, draft)

        package = await self._repository.approve(
            draft_id,
            telegram_user_id=user_id,
            build_package=lambda event, latest: build_publication_package(
                event, latest, approved_by=user_id, approved_at=self._clock()
            ),
        )
        if package is None:
            return DraftResult(DraftOutcome.STALE, draft)
        logger.info(
            "publication package stored",
            extra={"event_id": draft.event_id, "operation": "approve", "result": "outbox"},
        )
        return DraftResult(DraftOutcome.APPROVED, draft, package)

    async def approved_packages(self, *, limit: int = 10) -> list[PublicationPackage]:
        return await self._repository.list_packages(OutboxStatus.APPROVED, limit=limit)

    async def _stale(self, draft: Draft) -> DraftResult | None:
        latest = await self._repository.latest_draft(draft.event_id)
        if latest is None or latest.draft_id != draft.draft_id:
            return DraftResult(DraftOutcome.STALE, latest)
        if latest.status is not DraftStatus.ACTIVE:
            return DraftResult(DraftOutcome.STALE, latest)
        return None

    async def _write(
        self,
        card: ScoredEvent,
        *,
        version: int,
        previous: Draft | None = None,
        instruction: str | None = None,
    ) -> Draft | None:
        log = {"event_id": card.event.event_id, "operation": f"draft_v{version}"}
        try:
            text = await self._writer.write(card, previous=previous, instruction=instruction)
            draft = build_draft(card, text, version=version, created_at=self._clock())
        except (DraftFailed, ValueError) as exc:
            code = exc.code if isinstance(exc, DraftFailed) else "invalid_draft"
            logger.warning("draft failed", extra={**log, "result": "failed", "error_code": code})
            return None
        logger.info("draft created", extra={**log, "result": draft.fact_check_status.value})
        return draft.model_copy(update={"revision_instruction": instruction})


def build_draft(card: ScoredEvent, text: DraftText, *, version: int, created_at: datetime) -> Draft:
    """Apply the source-bound rules; raises ValueError when the text breaks them."""
    event = card.event
    if not quote_in_source(text.quote_text, event.original_text):
        raise ValueError("quote_text is not an exact fragment of the source post")
    notes = list(text.fact_check_notes)
    needs_review = text.fact_check_required or card.score.fact_check_required
    if card.score.fact_check_required and card.score.fact_check_note:
        notes.append(card.score.fact_check_note)

    author = _source_author(event)
    speaker = (text.quote_speaker or "").strip()
    if speaker and not quote_in_source(speaker, author):
        if not quote_in_source(speaker, event.original_text):
            raise ValueError("quote_speaker must be written in the source post")
        # The post author reports someone else's words: the attribution needs a human check.
        author, needs_review = speaker, True
        notes.append(f"Проверьте, что цитата действительно принадлежит: {speaker}.")

    return Draft(
        event_id=event.event_id,
        version=version,
        quote_text=normalize_for_quote(text.quote_text).strip(_TRIM),
        quote_author=author,
        quote_language=event.language or "und",
        context_summary=text.context_summary,
        qmemo_text=text.qmemo_text,
        x_text_template=text.x_text_template,
        x_text_short=text.x_text_short,
        angle=text.angle,
        cta=text.cta,
        fact_check_status=(
            FactCheckStatus.NEEDS_REVIEW if needs_review else FactCheckStatus.VERIFIED
        ),
        fact_check_notes=tuple(dict.fromkeys(notes)),
        prompt_version=text.prompt_version,
        model_name=text.model_name,
        created_at=created_at,
    )


def _source_author(event: EventCandidate) -> str:
    if event.author_display_name and event.author_handle:
        return f"{event.author_display_name} (@{event.author_handle})"
    if event.author_handle:
        return f"@{event.author_handle}"
    return event.author_display_name or "Unknown author"
