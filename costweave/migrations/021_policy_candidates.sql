CREATE TABLE IF NOT EXISTS policy_candidates (
    candidate_id TEXT PRIMARY KEY,
    predictor_artifact_id TEXT NOT NULL,
    predictor_evaluation_report_id TEXT NOT NULL,
    dataset_content_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status = 'offline_candidate'),
    eligible_for_offline_bandit INTEGER NOT NULL
        CHECK (eligible_for_offline_bandit = 1),
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    default_policy INTEGER NOT NULL CHECK (default_policy = 0),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL
        CHECK (may_change_actual_route = 0),
    automatic_promotion_allowed INTEGER NOT NULL
        CHECK (automatic_promotion_allowed = 0),
    commercial_claims_allowed INTEGER NOT NULL
        CHECK (commercial_claims_allowed = 0),
    candidate_json TEXT NOT NULL,
    candidate_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (predictor_artifact_id)
        REFERENCES predictor_candidate_artifacts(artifact_id) ON DELETE RESTRICT,
    FOREIGN KEY (predictor_evaluation_report_id)
        REFERENCES predictor_evaluation_reports(report_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_policy_candidates_status
    ON policy_candidates(status, created_at);

CREATE TABLE IF NOT EXISTS offline_bandit_evaluations (
    evaluation_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    observation_set_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'blocked_scope', 'blocked_support', 'blocked_evidence',
        'insufficient_effective_sample', 'offline_directional',
        'shadow_review_candidate'
    )),
    common_support INTEGER NOT NULL,
    counterfactual_conclusion_allowed INTEGER NOT NULL,
    eligible_for_v057_shadow_review INTEGER NOT NULL,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL
        CHECK (may_change_actual_route = 0),
    automatic_promotion_allowed INTEGER NOT NULL
        CHECK (automatic_promotion_allowed = 0),
    commercial_claims_allowed INTEGER NOT NULL
        CHECK (commercial_claims_allowed = 0),
    evaluation_json TEXT NOT NULL,
    evaluation_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id)
        REFERENCES policy_candidates(candidate_id) ON DELETE CASCADE,
    UNIQUE (candidate_id, observation_set_hash)
);

CREATE INDEX IF NOT EXISTS idx_bandit_evaluation_state
    ON offline_bandit_evaluations(
        state, counterfactual_conclusion_allowed,
        eligible_for_v057_shadow_review, created_at
    );

CREATE TABLE IF NOT EXISTS policy_freeze_assessments (
    assessment_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    signals_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('clear', 'frozen')),
    frozen INTEGER NOT NULL,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL
        CHECK (may_change_actual_route = 0),
    automatic_promotion_allowed INTEGER NOT NULL
        CHECK (automatic_promotion_allowed = 0),
    assessment_json TEXT NOT NULL,
    assessment_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id)
        REFERENCES policy_candidates(candidate_id) ON DELETE CASCADE,
    UNIQUE (candidate_id, signals_hash)
);

CREATE INDEX IF NOT EXISTS idx_policy_freeze_state
    ON policy_freeze_assessments(state, frozen, created_at);

CREATE TABLE IF NOT EXISTS policy_cards (
    card_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    bandit_evaluation_id TEXT NOT NULL,
    freeze_assessment_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('blocked', 'frozen', 'offline_review')),
    eligible_for_v057_review INTEGER NOT NULL,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL
        CHECK (may_change_actual_route = 0),
    automatic_promotion_allowed INTEGER NOT NULL
        CHECK (automatic_promotion_allowed = 0),
    commercial_claims_allowed INTEGER NOT NULL
        CHECK (commercial_claims_allowed = 0),
    card_json TEXT NOT NULL,
    card_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id)
        REFERENCES policy_candidates(candidate_id) ON DELETE CASCADE,
    FOREIGN KEY (bandit_evaluation_id)
        REFERENCES offline_bandit_evaluations(evaluation_id) ON DELETE CASCADE,
    FOREIGN KEY (freeze_assessment_id)
        REFERENCES policy_freeze_assessments(assessment_id) ON DELETE CASCADE,
    UNIQUE (candidate_id, bandit_evaluation_id, freeze_assessment_id)
);

CREATE INDEX IF NOT EXISTS idx_policy_card_status
    ON policy_cards(status, eligible_for_v057_review, created_at);

CREATE TABLE IF NOT EXISTS policy_candidate_deletions (
    candidate_id TEXT PRIMARY KEY,
    candidate_hash TEXT NOT NULL,
    bundle_hash TEXT NOT NULL,
    deleted_record_count INTEGER NOT NULL CHECK (deleted_record_count > 0),
    reason_code TEXT NOT NULL CHECK (reason_code IN (
        'user_request', 'superseded', 'governance_failure', 'test_cleanup'
    )),
    deleted_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS trg_policy_candidate_delete_requires_tombstone
BEFORE DELETE ON policy_candidates
WHEN NOT EXISTS (
    SELECT 1 FROM policy_candidate_deletions
    WHERE candidate_id = OLD.candidate_id
)
BEGIN
    SELECT RAISE(ABORT, 'policy candidate deletion requires an audit tombstone');
END;

CREATE TRIGGER IF NOT EXISTS trg_policy_candidate_immutable
BEFORE UPDATE ON policy_candidates
BEGIN
    SELECT RAISE(ABORT, 'policy candidates are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_bandit_evaluation_immutable
BEFORE UPDATE ON offline_bandit_evaluations
BEGIN
    SELECT RAISE(ABORT, 'offline bandit evaluations are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_policy_freeze_immutable
BEFORE UPDATE ON policy_freeze_assessments
BEGIN
    SELECT RAISE(ABORT, 'policy freeze assessments are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_policy_card_immutable
BEFORE UPDATE ON policy_cards
BEGIN
    SELECT RAISE(ABORT, 'policy cards are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_policy_deletion_update_immutable
BEFORE UPDATE ON policy_candidate_deletions
BEGIN
    SELECT RAISE(ABORT, 'policy deletion tombstones are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_policy_deletion_delete_immutable
BEFORE DELETE ON policy_candidate_deletions
BEGIN
    SELECT RAISE(ABORT, 'policy deletion tombstones are immutable');
END;
