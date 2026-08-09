CREATE TABLE IF NOT EXISTS release_attestations_v0584 (
    attestation_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    manifest_json TEXT NOT NULL CHECK(json_valid(manifest_json)),
    manifest_hash TEXT NOT NULL CHECK(length(manifest_hash)=64),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_release_attestations_v0584_project_created
ON release_attestations_v0584(project_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS release_attestations_v0584_no_update
BEFORE UPDATE ON release_attestations_v0584
BEGIN SELECT RAISE(ABORT, 'release attestations are immutable'); END;

CREATE TRIGGER IF NOT EXISTS release_attestations_v0584_no_delete
BEFORE DELETE ON release_attestations_v0584
BEGIN SELECT RAISE(ABORT, 'release attestations are immutable'); END;
