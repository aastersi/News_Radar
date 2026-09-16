import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

import aiosqlite

from qmemo_radar.domain import Engagement, EventCandidate, EventStatus, ScoreResult


class SQLiteEventRepository:
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    async def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        migration = (
            files("qmemo_radar.infrastructure.storage.migrations")
            .joinpath("001_initial.sql")
            .read_text(encoding="utf-8")
        )
        async with self._connect() as db:
            await db.executescript(migration)
            await db.commit()

    async def add_event(self, event: EventCandidate) -> bool:
        now = datetime.now(UTC).isoformat()
        async with self._connect() as db:
            cursor = await db.execute(
                """
                INSERT OR IGNORE INTO radar_events (
                    id, source, external_id, url, author_id, author_handle,
                    author_display_name, original_text, normalized_text,
                    content_hash, language, published_at, discovered_at,
                    engagement_json, raw_payload_json, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.source.value,
                    event.external_id,
                    str(event.url),
                    event.author_id,
                    event.author_handle,
                    event.author_display_name,
                    event.original_text,
                    event.normalized_text,
                    event.content_hash,
                    event.language,
                    event.published_at.isoformat(),
                    event.discovered_at.isoformat(),
                    event.engagement.model_dump_json(),
                    json.dumps(event.raw_payload, ensure_ascii=False, default=str),
                    event.status.value,
                    now,
                    now,
                ),
            )
            await db.commit()
            return cursor.rowcount == 1

    async def set_status(
        self,
        event_id: str,
        status: EventStatus,
        *,
        expected: set[EventStatus] | None = None,
        filter_reason: str | None = None,
    ) -> bool:
        parameters: list[object] = [
            status.value,
            filter_reason,
            datetime.now(UTC).isoformat(),
            event_id,
        ]
        query = """
            UPDATE radar_events
            SET status = ?, filter_reason = COALESCE(?, filter_reason), updated_at = ?
            WHERE id = ?
        """
        if expected:
            values = sorted(item.value for item in expected)
            placeholders = ",".join("?" for _ in values)
            query += f" AND status IN ({placeholders})"
            parameters.extend(values)

        async with self._connect() as db:
            cursor = await db.execute(query, parameters)
            await db.commit()
            return cursor.rowcount == 1

    async def list_events_by_status(
        self,
        status: EventStatus,
        *,
        limit: int,
    ) -> list[EventCandidate]:
        async with self._connect() as db:
            cursor = await db.execute(
                """
                SELECT * FROM radar_events
                WHERE status = ?
                ORDER BY discovered_at ASC
                LIMIT ?
                """,
                (status.value, limit),
            )
            rows = await cursor.fetchall()
        return [self._event_from_row(row) for row in rows]

    async def save_score_and_status(
        self,
        score: ScoreResult,
        status: EventStatus,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        breakdown = score.breakdown
        async with self._connect() as db:
            await db.execute("BEGIN IMMEDIATE")
            await db.execute(
                """
                INSERT INTO event_scores (
                    event_id, qmemo_relevance, quote_strength, discussion_potential,
                    freshness, clarity, action_likelihood, risk_penalty, total,
                    rationale, recommended_format, target_action,
                    fact_check_required, fact_check_note, prompt_version,
                    model_name, scored_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    qmemo_relevance = excluded.qmemo_relevance,
                    quote_strength = excluded.quote_strength,
                    discussion_potential = excluded.discussion_potential,
                    freshness = excluded.freshness,
                    clarity = excluded.clarity,
                    action_likelihood = excluded.action_likelihood,
                    risk_penalty = excluded.risk_penalty,
                    total = excluded.total,
                    rationale = excluded.rationale,
                    recommended_format = excluded.recommended_format,
                    target_action = excluded.target_action,
                    fact_check_required = excluded.fact_check_required,
                    fact_check_note = excluded.fact_check_note,
                    prompt_version = excluded.prompt_version,
                    model_name = excluded.model_name,
                    scored_at = excluded.scored_at
                """,
                (
                    score.event_id,
                    breakdown.qmemo_relevance,
                    breakdown.quote_strength,
                    breakdown.discussion_potential,
                    breakdown.freshness,
                    breakdown.clarity,
                    breakdown.action_likelihood,
                    breakdown.risk_penalty,
                    score.total,
                    score.rationale,
                    score.recommended_format,
                    score.target_action,
                    int(score.fact_check_required),
                    score.fact_check_note,
                    score.prompt_version,
                    score.model_name,
                    now,
                ),
            )
            cursor = await db.execute(
                """
                UPDATE radar_events
                SET status = ?, updated_at = ?
                WHERE id = ? AND status = ?
                """,
                (
                    status.value,
                    now,
                    score.event_id,
                    EventStatus.DISCOVERED.value,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Event {score.event_id} is not in DISCOVERED state"
                )
            await db.commit()

    async def count_by_status(self) -> dict[str, int]:
        async with self._connect() as db:
            cursor = await db.execute(
                "SELECT status, COUNT(*) AS amount FROM radar_events GROUP BY status"
            )
            rows = await cursor.fetchall()
            return {str(row[0]): int(row[1]) for row in rows}

    @staticmethod
    def _event_from_row(row: aiosqlite.Row) -> EventCandidate:
        values = dict(row)
        return EventCandidate.model_validate(
            {
                "event_id": values["id"],
                "source": values["source"],
                "external_id": values["external_id"],
                "url": values["url"],
                "author_id": values["author_id"],
                "author_handle": values["author_handle"],
                "author_display_name": values["author_display_name"],
                "original_text": values["original_text"],
                "normalized_text": values["normalized_text"],
                "content_hash": values["content_hash"],
                "language": values["language"],
                "published_at": values["published_at"],
                "discovered_at": values["discovered_at"],
                "engagement": Engagement.model_validate_json(values["engagement_json"]),
                "raw_payload": json.loads(values["raw_payload_json"]),
                "status": values["status"],
            }
        )

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[aiosqlite.Connection]:
        db = await aiosqlite.connect(self._db_path)
        try:
            db.row_factory = aiosqlite.Row
            await db.execute("PRAGMA journal_mode = WAL")
            await db.execute("PRAGMA foreign_keys = ON")
            await db.execute("PRAGMA busy_timeout = 5000")
            await db.execute("PRAGMA synchronous = NORMAL")
            yield db
        finally:
            await db.close()
