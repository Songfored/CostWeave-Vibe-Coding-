CREATE TABLE IF NOT EXISTS predictor_admission_reports (
    report_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    dataset_content_hash TEXT NOT NULL,
    profile TEXT NOT NULL,
    state TEXT NOT NULL,
    training_allowed INTEGER NOT NULL,
    policy_promotion_allowed INTEGER NOT NULL,
    summary_hash TEXT NOT NULL,
    report_json TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (dataset_id)
        REFERENCES outcome_datasets(dataset_id) ON DELETE RESTRICT,
    UNIQUE (dataset_id, dataset_content_hash, profile)
);

CREATE INDEX IF NOT EXISTS idx_predictor_admission_state
    ON predictor_admission_reports(state, profile, created_at);

CREATE TABLE IF NOT EXISTS frozen_prediction_receipts (
    receipt_id TEXT PRIMARY KEY,
    feature_fingerprint TEXT NOT NULL,
    admission_report_id TEXT,
    status TEXT NOT NULL,
    learned INTEGER NOT NULL,
    routing_authority INTEGER NOT NULL,
    receipt_json TEXT NOT NULL,
    receipt_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (admission_report_id)
        REFERENCES predictor_admission_reports(report_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_frozen_prediction_replay
    ON frozen_prediction_receipts(feature_fingerprint, created_at);
