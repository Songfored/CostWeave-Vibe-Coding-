ALTER TABLE messages
    ADD COLUMN contract_hash TEXT NOT NULL DEFAULT '';

ALTER TABLE messages
    ADD COLUMN preflight_json TEXT NOT NULL DEFAULT '{}';

ALTER TABLE failure_records
    ADD COLUMN recovery_receipt_json TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_messages_run_admission
    ON messages(run_id, plan_revision, created_at);
