BEGIN;

-- Retention checks look up child rows by event id; without these every candidate scans a table.
CREATE INDEX idx_events_duplicate_of ON radar_events(duplicate_of_event_id)
WHERE duplicate_of_event_id IS NOT NULL;
CREATE INDEX idx_feedback_event ON feedback(event_id);
CREATE INDEX idx_outbox_event ON publication_outbox(event_id);

INSERT INTO schema_migrations(version, applied_at)
VALUES (8, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
