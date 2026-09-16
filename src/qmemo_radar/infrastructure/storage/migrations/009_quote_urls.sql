-- GDELT stores one row per quote, so several rows of one source share an article URL.
-- SQLite cannot drop the table-level UNIQUE(source, url), so the table is rebuilt with the same
-- columns (https://sqlite.org/lang_altertable.html#otheralter) and the URL rule becomes a partial
-- unique index for every other source. Child tables keep referencing radar_events(id).
PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE radar_events_new (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    external_id TEXT NOT NULL,
    url TEXT NOT NULL,
    author_id TEXT,
    author_handle TEXT,
    author_display_name TEXT,
    original_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    language TEXT,
    published_at TEXT NOT NULL,
    discovered_at TEXT NOT NULL,
    engagement_json TEXT NOT NULL DEFAULT '{}',
    raw_payload_json TEXT NOT NULL DEFAULT '{}',
    duplicate_of_event_id TEXT,
    filter_reason TEXT,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    source_key TEXT,
    UNIQUE(source, external_id),
    FOREIGN KEY(duplicate_of_event_id) REFERENCES radar_events(id)
);

-- rowid is copied: "earliest original" lookups order by it.
INSERT INTO radar_events_new (
    rowid, id, source, external_id, url, author_id, author_handle, author_display_name,
    original_text, normalized_text, content_hash, language, published_at, discovered_at,
    engagement_json, raw_payload_json, duplicate_of_event_id, filter_reason, status,
    created_at, updated_at, source_key
)
SELECT
    rowid, id, source, external_id, url, author_id, author_handle, author_display_name,
    original_text, normalized_text, content_hash, language, published_at, discovered_at,
    engagement_json, raw_payload_json, duplicate_of_event_id, filter_reason, status,
    created_at, updated_at, source_key
FROM radar_events;

DROP TABLE radar_events;
ALTER TABLE radar_events_new RENAME TO radar_events;

CREATE INDEX idx_events_status_published ON radar_events(status, published_at DESC);
CREATE INDEX idx_events_content_hash ON radar_events(content_hash);
CREATE INDEX idx_events_duplicate_of ON radar_events(duplicate_of_event_id)
WHERE duplicate_of_event_id IS NOT NULL;
-- Keep in sync with SHARED_URL_SOURCES in domain/enums.py.
CREATE UNIQUE INDEX idx_events_source_url ON radar_events(source, url) WHERE source != 'gdelt';

INSERT INTO schema_migrations(version, applied_at)
VALUES (9, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;

PRAGMA foreign_keys = ON;
