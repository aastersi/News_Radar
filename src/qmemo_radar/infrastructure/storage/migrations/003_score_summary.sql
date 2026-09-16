BEGIN;

ALTER TABLE event_scores ADD COLUMN headline TEXT NOT NULL DEFAULT '';
ALTER TABLE event_scores ADD COLUMN summary TEXT NOT NULL DEFAULT '';

INSERT INTO schema_migrations(version, applied_at)
VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
