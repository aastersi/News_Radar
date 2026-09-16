BEGIN;

-- One row per run, source and metric. New metric names need no migration.
CREATE TABLE pipeline_metrics (
    run_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    metric TEXT NOT NULL,
    value INTEGER NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (run_id, source_key, metric)
);

CREATE INDEX idx_pipeline_metrics_recorded_at ON pipeline_metrics(recorded_at);

INSERT INTO schema_migrations(version, applied_at)
VALUES (7, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
