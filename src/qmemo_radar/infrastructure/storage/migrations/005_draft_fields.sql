BEGIN;

ALTER TABLE drafts ADD COLUMN quote_author TEXT NOT NULL DEFAULT '';
ALTER TABLE drafts ADD COLUMN quote_language TEXT NOT NULL DEFAULT 'und';
ALTER TABLE drafts ADD COLUMN model_name TEXT NOT NULL DEFAULT '';
ALTER TABLE drafts ADD COLUMN revision_instruction TEXT;

INSERT INTO schema_migrations(version, applied_at)
VALUES (5, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
