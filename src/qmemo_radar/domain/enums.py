from enum import StrEnum


class SourceType(StrEnum):
    X = "x"
    RSS = "rss"
    MANUAL = "manual"


class EventStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    FILTERED_OUT = "FILTERED_OUT"
    SCORED = "SCORED"
    SHORTLISTED = "SHORTLISTED"
    NOTIFIED = "NOTIFIED"
    SNOOZED = "SNOOZED"
    SKIPPED = "SKIPPED"
    ARCHIVED = "ARCHIVED"
    DRAFTED = "DRAFTED"
    APPROVED = "APPROVED"
    EXPIRED = "EXPIRED"


class OutboxStatus(StrEnum):
    APPROVED = "APPROVED"
    QMEMO_PENDING = "QMEMO_PENDING"
    QMEMO_PUBLISHED = "QMEMO_PUBLISHED"
    X_PENDING = "X_PENDING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    VERIFY_REQUIRED = "VERIFY_REQUIRED"


class FactCheckStatus(StrEnum):
    VERIFIED = "VERIFIED"
    NEEDS_REVIEW = "NEEDS_REVIEW"



class DeliveryKind(StrEnum):
    DIGEST = "digest"
    URGENT = "urgent"


class FeedbackAction(StrEnum):
    USE = "USE"
    SKIP = "SKIP"
    LATER = "LATER"
    REVISE_SHORTER = "REVISE_SHORTER"
    REVISE_ANGLE = "REVISE_ANGLE"
    REVISE_CUSTOM = "REVISE_CUSTOM"
    VERIFY = "VERIFY"
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"


class RunStatus(StrEnum):
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class DraftStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
