CREATE TABLE IF NOT EXISTS production_shadow_inputs (
    input_id TEXT PRIMARY KEY,
    input_hash TEXT NOT NULL UNIQUE,
    task_feature_hash TEXT NOT NULL,
    catalog_snapshot_hash TEXT NOT NULL,
    capacity_snapshot_hash TEXT NOT NULL,
    actual_route_id TEXT NOT NULL,
    actual_route_fingerprint TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    data_origin TEXT NOT NULL,
    same_features_as_baseline INTEGER NOT NULL CHECK (same_features_as_baseline = 1),
    same_catalog_as_baseline INTEGER NOT NULL CHECK (same_catalog_as_baseline = 1),
    same_capacity_as_baseline INTEGER NOT NULL CHECK (same_capacity_as_baseline = 1),
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL CHECK (may_change_actual_route = 0),
    input_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS production_shadow_decisions (
    decision_id TEXT PRIMARY KEY,
    input_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('advice_generated','advice_missing')),
    missing_reason TEXT,
    baseline_execution_continues INTEGER NOT NULL CHECK (baseline_execution_continues = 1),
    actual_route_changed INTEGER NOT NULL CHECK (actual_route_changed = 0),
    external_call_count INTEGER NOT NULL CHECK (external_call_count = 0),
    execution_side_effect_count INTEGER NOT NULL CHECK (execution_side_effect_count = 0),
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    policy_promotion_allowed INTEGER NOT NULL CHECK (policy_promotion_allowed = 0),
    decision_json TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (input_id) REFERENCES production_shadow_inputs(input_id)
        ON DELETE RESTRICT,
    UNIQUE (input_id, policy_id, policy_version)
);

CREATE TABLE IF NOT EXISTS production_shadow_failures (
    failure_id TEXT PRIMARY KEY,
    input_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    error_code TEXT NOT NULL CHECK (error_code IN ('validation_error','shadow_internal_error')),
    baseline_execution_continues INTEGER NOT NULL CHECK (baseline_execution_continues = 1),
    actual_route_changed INTEGER NOT NULL CHECK (actual_route_changed = 0),
    error_message_persisted INTEGER NOT NULL CHECK (error_message_persisted = 0),
    external_call_count INTEGER NOT NULL CHECK (external_call_count = 0),
    execution_side_effect_count INTEGER NOT NULL CHECK (execution_side_effect_count = 0),
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    failure_json TEXT NOT NULL,
    failure_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (input_id) REFERENCES production_shadow_inputs(input_id)
        ON DELETE RESTRICT,
    UNIQUE (input_id, policy_id, policy_version)
);

CREATE TABLE IF NOT EXISTS production_shadow_outcomes (
    outcome_id TEXT PRIMARY KEY,
    input_id TEXT NOT NULL UNIQUE,
    actual_route_id TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    data_origin TEXT NOT NULL,
    independently_validated INTEGER NOT NULL,
    outcome_json TEXT NOT NULL,
    outcome_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (input_id) REFERENCES production_shadow_inputs(input_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS production_shadow_comparisons (
    comparison_id TEXT PRIMARY KEY,
    input_id TEXT NOT NULL UNIQUE,
    decision_id TEXT NOT NULL UNIQUE,
    outcome_id TEXT NOT NULL UNIQUE,
    counterfactual_outcome_known INTEGER NOT NULL,
    counterfactual_savings_claim_allowed INTEGER NOT NULL
        CHECK (counterfactual_savings_claim_allowed = 0),
    actual_route_changed INTEGER NOT NULL CHECK (actual_route_changed = 0),
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    comparison_json TEXT NOT NULL,
    comparison_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (input_id) REFERENCES production_shadow_inputs(input_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (decision_id) REFERENCES production_shadow_decisions(decision_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (outcome_id) REFERENCES production_shadow_outcomes(outcome_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS production_shadow_audit_reports (
    report_id TEXT PRIMARY KEY,
    batch_hash TEXT NOT NULL UNIQUE,
    state TEXT NOT NULL CHECK (state IN (
        'blocked_evidence','shadow_directional','shadow_overhead_exceeded'
    )),
    directional_evidence_eligible INTEGER NOT NULL,
    cost_overhead_principle_passed INTEGER NOT NULL,
    baseline_execution_continuity_proven INTEGER NOT NULL
        CHECK (baseline_execution_continuity_proven = 1),
    execution_side_effect_count INTEGER NOT NULL CHECK (execution_side_effect_count = 0),
    actual_route_change_count INTEGER NOT NULL CHECK (actual_route_change_count = 0),
    external_call_count INTEGER NOT NULL CHECK (external_call_count = 0),
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    policy_promotion_allowed INTEGER NOT NULL CHECK (policy_promotion_allowed = 0),
    commercial_claims_allowed INTEGER NOT NULL CHECK (commercial_claims_allowed = 0),
    report_json TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_production_shadow_decision_state
    ON production_shadow_decisions(state, missing_reason, created_at);
CREATE INDEX IF NOT EXISTS idx_production_shadow_outcome_evidence
    ON production_shadow_outcomes(evidence_level, data_origin, independently_validated);
CREATE INDEX IF NOT EXISTS idx_production_shadow_audit_state
    ON production_shadow_audit_reports(state, directional_evidence_eligible);

CREATE TRIGGER IF NOT EXISTS trg_shadow_decision_excludes_failure
BEFORE INSERT ON production_shadow_decisions
WHEN EXISTS (
    SELECT 1 FROM production_shadow_failures
    WHERE input_id=NEW.input_id AND policy_id=NEW.policy_id
        AND policy_version=NEW.policy_version
)
BEGIN
    SELECT RAISE(ABORT, 'shadow input already has an isolated failure');
END;

CREATE TRIGGER IF NOT EXISTS trg_shadow_failure_excludes_decision
BEFORE INSERT ON production_shadow_failures
WHEN EXISTS (
    SELECT 1 FROM production_shadow_decisions
    WHERE input_id=NEW.input_id AND policy_id=NEW.policy_id
        AND policy_version=NEW.policy_version
)
BEGIN
    SELECT RAISE(ABORT, 'shadow input already has a decision');
END;

CREATE TRIGGER IF NOT EXISTS trg_production_shadow_input_immutable
BEFORE UPDATE ON production_shadow_inputs BEGIN
    SELECT RAISE(ABORT, 'production shadow inputs are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_input_delete_immutable
BEFORE DELETE ON production_shadow_inputs BEGIN
    SELECT RAISE(ABORT, 'production shadow inputs cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_decision_immutable
BEFORE UPDATE ON production_shadow_decisions BEGIN
    SELECT RAISE(ABORT, 'production shadow decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_decision_delete_immutable
BEFORE DELETE ON production_shadow_decisions BEGIN
    SELECT RAISE(ABORT, 'production shadow decisions cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_failure_immutable
BEFORE UPDATE ON production_shadow_failures BEGIN
    SELECT RAISE(ABORT, 'production shadow failures are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_failure_delete_immutable
BEFORE DELETE ON production_shadow_failures BEGIN
    SELECT RAISE(ABORT, 'production shadow failures cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_outcome_immutable
BEFORE UPDATE ON production_shadow_outcomes BEGIN
    SELECT RAISE(ABORT, 'production shadow outcomes are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_outcome_delete_immutable
BEFORE DELETE ON production_shadow_outcomes BEGIN
    SELECT RAISE(ABORT, 'production shadow outcomes cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_comparison_immutable
BEFORE UPDATE ON production_shadow_comparisons BEGIN
    SELECT RAISE(ABORT, 'production shadow comparisons are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_comparison_delete_immutable
BEFORE DELETE ON production_shadow_comparisons BEGIN
    SELECT RAISE(ABORT, 'production shadow comparisons cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_audit_immutable
BEFORE UPDATE ON production_shadow_audit_reports BEGIN
    SELECT RAISE(ABORT, 'production shadow audit reports are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_production_shadow_audit_delete_immutable
BEFORE DELETE ON production_shadow_audit_reports BEGIN
    SELECT RAISE(ABORT, 'production shadow audit reports cannot be deleted');
END;
