BEGIN;

ALTER TABLE radar_events ADD COLUMN source_key TEXT;

INSERT INTO schema_migrations(version, applied_at)
VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
