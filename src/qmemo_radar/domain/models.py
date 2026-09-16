from datetime import datetime
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from qmemo_radar.domain.enums import (
    EventStatus,
    FactCheckStatus,
    OutboxStatus,
    SourceType,
)


class DomainModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Engagement(DomainModel):
    likes: int = Field(default=0, ge=0)
    reposts: int = Field(default=0, ge=0)
    replies: int = Field(default=0, ge=0)
    quotes: int = Field(default=0, ge=0)
    views: int | None = Field(default=None, ge=0)


class RawSourceItem(DomainModel):
    source: SourceType
    external_id: str = Field(min_length=1, max_length=255)
    url: HttpUrl
    author_id: str | None = None
    author_handle: str | None = None
    author_display_name: str | None = None
    original_text: str = Field(min_length=1)
    language: str | None = None
    published_at: datetime
    engagement: Engagement = Field(default_factory=Engagement)
    raw_payload: dict[str, object] = Field(default_factory=dict)


class EventCandidate(RawSourceItem):
    event_id: str = Field(default_factory=lambda: uuid4().hex)
    discovered_at: datetime
    normalized_text: str
    content_hash: str
    status: EventStatus = EventStatus.DISCOVERED


class ScoreBreakdown(DomainModel):
    qmemo_relevance: int = Field(ge=0, le=30)
    quote_strength: int = Field(ge=0, le=20)
    discussion_potential: int = Field(ge=0, le=15)
    freshness: int = Field(ge=0, le=15)
    clarity: int = Field(ge=0, le=10)
    action_likelihood: int = Field(ge=0, le=10)
    risk_penalty: int = Field(ge=0, le=30)


class ScoreResult(DomainModel):
    event_id: str
    breakdown: ScoreBreakdown
    total: int = Field(ge=0, le=100)
    rationale: str = Field(min_length=1)
    recommended_format: str = Field(min_length=1)
    target_action: str = Field(min_length=1)
    fact_check_required: bool = False
    fact_check_note: str | None = None
    prompt_version: str = "deterministic-v1"
    model_name: str = "none"


class PublicationPackage(DomainModel):
    schema_version: int = 1
    package_id: str = Field(default_factory=lambda: uuid4().hex)
    event_id: str
    draft_id: str
    quote_text: str
    quote_author: str
    quote_language: str
    category_hint: str | None = None
    context_summary: str
    qmemo_text: str
    source_type: SourceType
    source_external_id: str
    source_url: HttpUrl
    source_published_at: datetime
    x_text_template: str
    x_text_short: str
    cta: str
    fact_check_status: FactCheckStatus
    fact_check_notes: tuple[str, ...] = ()
    approved_by_telegram_id: int
    approved_at: datetime
    idempotency_key: str
    status: OutboxStatus = OutboxStatus.APPROVED


class PipelineCounters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    collected: int = 0
    inserted: int = 0
    duplicates: int = 0
    filtered: int = 0
    scored: int = 0
    shortlisted: int = 0
    archived: int = 0

