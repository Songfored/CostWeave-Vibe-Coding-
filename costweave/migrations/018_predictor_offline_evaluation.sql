CREATE TABLE IF NOT EXISTS predictor_candidate_artifacts (
    artifact_id TEXT PRIMARY KEY,
    admission_report_id TEXT NOT NULL,
    dataset_content_hash TEXT NOT NULL,
    method TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'frozen_candidate'),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    policy_promotion_allowed INTEGER NOT NULL
        CHECK (policy_promotion_allowed = 0),
    artifact_json TEXT NOT NULL,
    artifact_hash TEXT NOT NULL,
    model_payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (admission_report_id)
        REFERENCES predictor_admission_reports(report_id) ON DELETE RESTRICT,
    UNIQUE (admission_report_id, model_payload_hash)
);

CREATE INDEX IF NOT EXISTS idx_predictor_candidate_status
    ON predictor_candidate_artifacts(status, created_at);

CREATE TABLE IF NOT EXISTS predictor_evaluation_reports (
    report_id TEXT PRIMARY KEY,
    predictor_artifact_id TEXT NOT NULL,
    admission_report_id TEXT NOT NULL,
    cohort_hash TEXT NOT NULL,
    state TEXT NOT NULL,
    offline_evaluation_passed INTEGER NOT NULL,
    eligible_for_shadow INTEGER NOT NULL,
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    policy_promotion_allowed INTEGER NOT NULL
        CHECK (policy_promotion_allowed = 0),
    report_json TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (predictor_artifact_id)
        REFERENCES predictor_candidate_artifacts(artifact_id) ON DELETE RESTRICT,
    FOREIGN KEY (admission_report_id)
        REFERENCES predictor_admission_reports(report_id) ON DELETE RESTRICT,
    UNIQUE (predictor_artifact_id, cohort_hash)
);

CREATE INDEX IF NOT EXISTS idx_predictor_evaluation_state
    ON predictor_evaluation_reports(state, eligible_for_shadow, created_at);

CREATE TABLE IF NOT EXISTS predictor_challenger_comparisons (
    comparison_id TEXT PRIMARY KEY,
    champion_report_id TEXT NOT NULL,
    challenger_report_id TEXT NOT NULL,
    eligible_for_shadow_comparison INTEGER NOT NULL,
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    policy_promotion_allowed INTEGER NOT NULL
        CHECK (policy_promotion_allowed = 0),
    comparison_json TEXT NOT NULL,
    comparison_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (champion_report_id)
        REFERENCES predictor_evaluation_reports(report_id) ON DELETE RESTRICT,
    FOREIGN KEY (challenger_report_id)
        REFERENCES predictor_evaluation_reports(report_id) ON DELETE RESTRICT,
    UNIQUE (champion_report_id, challenger_report_id)
);

CREATE TRIGGER IF NOT EXISTS trg_predictor_admission_no_promotion_insert
BEFORE INSERT ON predictor_admission_reports
WHEN NEW.policy_promotion_allowed <> 0
BEGIN
    SELECT RAISE(ABORT, 'predictor admission cannot promote a policy');
END;

CREATE TRIGGER IF NOT EXISTS trg_predictor_admission_no_promotion_update
BEFORE UPDATE OF policy_promotion_allowed ON predictor_admission_reports
WHEN NEW.policy_promotion_allowed <> 0
BEGIN
    SELECT RAISE(ABORT, 'predictor admission cannot promote a policy');
END;

CREATE TRIGGER IF NOT EXISTS trg_frozen_prediction_no_authority_insert
BEFORE INSERT ON frozen_prediction_receipts
WHEN NEW.learned <> 0 OR NEW.routing_authority <> 0
BEGIN
    SELECT RAISE(ABORT, 'frozen prediction cannot become authoritative');
END;

CREATE TRIGGER IF NOT EXISTS trg_frozen_prediction_no_authority_update
BEFORE UPDATE OF learned, routing_authority ON frozen_prediction_receipts
WHEN NEW.learned <> 0 OR NEW.routing_authority <> 0
BEGIN
    SELECT RAISE(ABORT, 'frozen prediction cannot become authoritative');
END;
