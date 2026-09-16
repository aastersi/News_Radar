BEGIN;

-- Money is stored as integer micro-dollars so limits never suffer from float rounding.
CREATE TABLE cost_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider TEXT NOT NULL,
    operation TEXT NOT NULL,
    units INTEGER NOT NULL CHECK(units >= 0),
    estimated_cost_micros INTEGER NOT NULL CHECK(estimated_cost_micros >= 0),
    created_at TEXT NOT NULL
);

CREATE INDEX idx_cost_ledger_created_at ON cost_ledger(created_at);

INSERT INTO schema_migrations(version, applied_at)
VALUES (6, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
