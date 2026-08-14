ALTER TABLE evidence_export_receipts
ADD COLUMN project_id TEXT NOT NULL DEFAULT 'local-default';

CREATE INDEX IF NOT EXISTS idx_evidence_export_receipts_project_created
ON evidence_export_receipts(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS security_project_tombstones (
    project_id TEXT PRIMARY KEY,
    reason TEXT NOT NULL CHECK(reason IN ('user_requested','retention_expired','security_response')),
    idempotency_key_hash TEXT NOT NULL CHECK(length(idempotency_key_hash)=64),
    tombstone_json TEXT NOT NULL CHECK(json_valid(tombstone_json)),
    tombstone_hash TEXT NOT NULL UNIQUE CHECK(length(tombstone_hash)=64),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS external_side_effect_contracts (
    side_effect_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    adapter_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    idempotency_key_hash TEXT NOT NULL CHECK(length(idempotency_key_hash)=64),
    request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
    provider_idempotency_forwarded INTEGER NOT NULL CHECK(provider_idempotency_forwarded IN (0,1)),
    compensation_strategy TEXT,
    ambiguous_auto_replay INTEGER NOT NULL CHECK(ambiguous_auto_replay=0),
    contract_json TEXT NOT NULL CHECK(json_valid(contract_json)),
    contract_hash TEXT NOT NULL UNIQUE CHECK(length(contract_hash)=64),
    created_at TEXT NOT NULL,
    UNIQUE(project_id, idempotency_key_hash),
    CHECK(provider_idempotency_forwarded=1 OR compensation_strategy IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_external_side_effect_contracts_project
ON external_side_effect_contracts(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS external_side_effect_events (
    event_id TEXT PRIMARY KEY,
    side_effect_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('reserved','completed','failed','ambiguous','reconciled')),
    event_json TEXT NOT NULL CHECK(json_valid(event_json)),
    event_hash TEXT NOT NULL UNIQUE CHECK(length(event_hash)=64),
    created_at TEXT NOT NULL,
    FOREIGN KEY(side_effect_id) REFERENCES external_side_effect_contracts(side_effect_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_external_side_effect_events_contract
ON external_side_effect_events(side_effect_id, created_at, event_id);

CREATE TRIGGER IF NOT EXISTS security_project_tombstones_no_update
BEFORE UPDATE ON security_project_tombstones
BEGIN SELECT RAISE(ABORT, 'security project tombstones are immutable'); END;

CREATE TRIGGER IF NOT EXISTS security_project_tombstones_no_delete
BEFORE DELETE ON security_project_tombstones
BEGIN SELECT RAISE(ABORT, 'security project tombstones are immutable'); END;

CREATE TRIGGER IF NOT EXISTS external_side_effect_contracts_no_update
BEFORE UPDATE ON external_side_effect_contracts
BEGIN SELECT RAISE(ABORT, 'external side effect contracts are immutable'); END;

CREATE TRIGGER IF NOT EXISTS external_side_effect_contracts_no_delete
BEFORE DELETE ON external_side_effect_contracts
BEGIN SELECT RAISE(ABORT, 'external side effect contracts are immutable'); END;

CREATE TRIGGER IF NOT EXISTS external_side_effect_events_no_update
BEFORE UPDATE ON external_side_effect_events
BEGIN SELECT RAISE(ABORT, 'external side effect events are immutable'); END;

CREATE TRIGGER IF NOT EXISTS external_side_effect_events_no_delete
BEFORE DELETE ON external_side_effect_events
BEGIN SELECT RAISE(ABORT, 'external side effect events are immutable'); END;
