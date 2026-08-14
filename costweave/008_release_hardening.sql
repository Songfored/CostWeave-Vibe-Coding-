CREATE TABLE IF NOT EXISTS event_integrity (
    event_id TEXT PRIMARY KEY,
    payload_hash TEXT NOT NULL,
    envelope_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(event_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_integrity_payload_hash
    ON event_integrity(payload_hash);

CREATE TABLE IF NOT EXISTS media_commands (
    idempotency_key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    target_id TEXT NOT NULL,
    result_json TEXT NOT NULL,
    completed_at TEXT NOT NULL
);
