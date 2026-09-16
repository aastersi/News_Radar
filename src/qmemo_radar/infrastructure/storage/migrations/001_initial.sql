CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL,
    counters_json TEXT NOT NULL DEFAULT '{}',
    error_summary TEXT
);

CREATE TABLE IF NOT EXISTS source_checkpoints (
    source_key TEXT PRIMARY KEY,
    cursor_value TEXT,
    last_success_at TEXT,
    last_error_at TEXT,
    last_error TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS radar_events (
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
    UNIQUE(source, external_id),
    UNIQUE(source, url),
    FOREIGN KEY(duplicate_of_event_id) REFERENCES radar_events(id)
);

CREATE INDEX IF NOT EXISTS idx_events_status_published
ON radar_events(status, published_at DESC);

CREATE INDEX IF NOT EXISTS idx_events_content_hash
ON radar_events(content_hash);

CREATE TABLE IF NOT EXISTS event_scores (
    event_id TEXT PRIMARY KEY,
    qmemo_relevance INTEGER NOT NULL,
    quote_strength INTEGER NOT NULL,
    discussion_potential INTEGER NOT NULL,
    freshness INTEGER NOT NULL,
    clarity INTEGER NOT NULL,
    action_likelihood INTEGER NOT NULL,
    risk_penalty INTEGER NOT NULL,
    total INTEGER NOT NULL,
    rationale TEXT NOT NULL,
    recommended_format TEXT NOT NULL,
    target_action TEXT NOT NULL,
    fact_check_required INTEGER NOT NULL,
    fact_check_note TEXT,
    prompt_version TEXT NOT NULL,
    model_name TEXT NOT NULL,
    scored_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES radar_events(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS telegram_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    delivery_type TEXT NOT NULL,
    delivered_at TEXT NOT NULL,
    UNIQUE(chat_id, message_id),
    UNIQUE(event_id, delivery_type),
    FOREIGN KEY(event_id) REFERENCES radar_events(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS drafts (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version BETWEEN 1 AND 2),
    quote_text TEXT NOT NULL,
    context_summary TEXT NOT NULL,
    qmemo_text TEXT NOT NULL,
    x_text_template TEXT NOT NULL,
    x_text_short TEXT NOT NULL,
    angle TEXT NOT NULL,
    cta TEXT NOT NULL,
    fact_check_status TEXT NOT NULL,
    fact_check_notes_json TEXT NOT NULL DEFAULT '[]',
    prompt_version TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(event_id, version),
    FOREIGN KEY(event_id) REFERENCES radar_events(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL,
    draft_id TEXT,
    action TEXT NOT NULL,
    note TEXT,
    telegram_user_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES radar_events(id) ON DELETE CASCADE,
    FOREIGN KEY(draft_id) REFERENCES drafts(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS publication_outbox (
    id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL,
    draft_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status TEXT NOT NULL,
    qmemo_attempt_count INTEGER NOT NULL DEFAULT 0,
    x_attempt_count INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    qmemo_result_json TEXT,
    x_result_json TEXT,
    last_error_code TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(event_id) REFERENCES radar_events(id),
    FOREIGN KEY(draft_id) REFERENCES drafts(id)
);

CREATE INDEX IF NOT EXISTS idx_outbox_status_next_attempt
ON publication_outbox(status, next_attempt_at);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

