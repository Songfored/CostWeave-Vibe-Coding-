CREATE TABLE IF NOT EXISTS evidence_export_receipts (
    export_id TEXT PRIMARY KEY,
    run_id TEXT,
    scope TEXT NOT NULL CHECK(scope IN ('workspace','run')),
    schema_version TEXT NOT NULL CHECK(schema_version='v0.5.8.1-export'),
    package_hash TEXT NOT NULL UNIQUE CHECK(length(package_hash)=64),
    source_count INTEGER NOT NULL CHECK(source_count>=0),
    record_count INTEGER NOT NULL CHECK(record_count>=0),
    evidence_counts_json TEXT NOT NULL CHECK(json_valid(evidence_counts_json)),
    commercial_claims_allowed INTEGER NOT NULL CHECK(commercial_claims_allowed=0),
    receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
    receipt_hash TEXT NOT NULL UNIQUE CHECK(length(receipt_hash)=64),
    created_at TEXT NOT NULL,
    CHECK((scope='run' AND run_id IS NOT NULL) OR (scope='workspace' AND run_id IS NULL))
);

CREATE INDEX IF NOT EXISTS idx_evidence_export_receipts_scope_created
ON evidence_export_receipts(scope, created_at DESC);

CREATE TRIGGER IF NOT EXISTS evidence_export_receipts_no_update
BEFORE UPDATE ON evidence_export_receipts
BEGIN
    SELECT RAISE(ABORT, 'evidence export receipts are immutable');
END;

CREATE TRIGGER IF NOT EXISTS evidence_export_receipts_no_delete
BEFORE DELETE ON evidence_export_receipts
BEGIN
    SELECT RAISE(ABORT, 'evidence export receipts are immutable');
END;
