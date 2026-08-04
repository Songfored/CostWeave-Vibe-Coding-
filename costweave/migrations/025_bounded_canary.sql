CREATE TABLE IF NOT EXISTS canary_deployments (
    deployment_id TEXT PRIMARY KEY,
    candidate_policy_id TEXT NOT NULL,
    candidate_policy_version TEXT NOT NULL,
    stable_policy_id TEXT NOT NULL,
    stable_policy_version TEXT NOT NULL,
    canary_gate_report_id TEXT NOT NULL,
    activation_approval_id TEXT NOT NULL,
    initial_stage_pct INTEGER NOT NULL CHECK (initial_stage_pct = 5),
    automatic_rollback INTEGER NOT NULL CHECK (automatic_rollback = 1),
    high_risk_exploration_rate REAL NOT NULL CHECK (high_risk_exploration_rate = 0),
    provider_credentials_granted INTEGER NOT NULL CHECK (provider_credentials_granted = 0),
    tool_permissions_expanded INTEGER NOT NULL CHECK (tool_permissions_expanded = 0),
    deployment_json TEXT NOT NULL,
    deployment_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (stable_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (canary_gate_report_id) REFERENCES policy_promotion_gate_reports(gate_report_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS canary_runtime_pointer_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    deployment_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('started','advanced','rolled_back','completed')),
    stage_pct INTEGER NOT NULL CHECK (stage_pct IN (5,25,50,100)),
    selected_policy_id TEXT NOT NULL,
    decision_id TEXT,
    in_flight_snapshots_preserved INTEGER NOT NULL CHECK (in_flight_snapshots_preserved = 1),
    new_runs_use_pointer_immediately INTEGER NOT NULL CHECK (new_runs_use_pointer_immediately = 1),
    execution_side_effect_count INTEGER NOT NULL CHECK (execution_side_effect_count = 0),
    created_at TEXT NOT NULL,
    FOREIGN KEY (deployment_id) REFERENCES canary_deployments(deployment_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (selected_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS canary_run_assignments (
    assignment_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    run_id TEXT NOT NULL UNIQUE,
    stage_pct INTEGER NOT NULL CHECK (stage_pct IN (5,25,50,100)),
    bucket INTEGER NOT NULL CHECK (bucket >= 0 AND bucket < 10000),
    risk_level TEXT NOT NULL,
    eligible_for_exploration INTEGER NOT NULL,
    cohort TEXT NOT NULL CHECK (cohort IN ('candidate','control','stable-only')),
    selected_policy_id TEXT NOT NULL,
    selected_policy_version TEXT NOT NULL,
    policy_snapshot_hash TEXT NOT NULL,
    runtime_policy_changed INTEGER NOT NULL,
    provider_call_executed INTEGER NOT NULL CHECK (provider_call_executed = 0),
    assignment_json TEXT NOT NULL,
    assignment_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (deployment_id) REFERENCES canary_deployments(deployment_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (selected_policy_id) REFERENCES registered_policies(registry_policy_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS canary_observation_windows (
    window_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    stage_pct INTEGER NOT NULL CHECK (stage_pct IN (5,25,50,100)),
    candidate_cases INTEGER NOT NULL CHECK (candidate_cases >= 0),
    control_cases INTEGER NOT NULL CHECK (control_cases >= 0),
    hard_budget_violation_count INTEGER NOT NULL CHECK (hard_budget_violation_count >= 0),
    permission_violation_count INTEGER NOT NULL CHECK (permission_violation_count >= 0),
    duplicate_external_side_effect_count INTEGER NOT NULL CHECK (duplicate_external_side_effect_count >= 0),
    high_risk_candidate_cases INTEGER NOT NULL CHECK (high_risk_candidate_cases >= 0),
    observation_json TEXT NOT NULL,
    observation_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (deployment_id) REFERENCES canary_deployments(deployment_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS canary_stage_decisions (
    decision_id TEXT PRIMARY KEY,
    deployment_id TEXT NOT NULL,
    window_id TEXT NOT NULL UNIQUE,
    from_stage_pct INTEGER NOT NULL CHECK (from_stage_pct IN (5,25,50,100)),
    to_stage_pct INTEGER NOT NULL CHECK (to_stage_pct IN (5,25,50,100)),
    action TEXT NOT NULL CHECK (action IN ('hold','advance','rollback','complete')),
    runtime_pointer_changed INTEGER NOT NULL,
    automatic_rollback_executed INTEGER NOT NULL,
    in_flight_snapshots_preserved INTEGER NOT NULL CHECK (in_flight_snapshots_preserved = 1),
    new_runs_use_decision_immediately INTEGER NOT NULL CHECK (new_runs_use_decision_immediately = 1),
    execution_side_effect_count INTEGER NOT NULL CHECK (execution_side_effect_count = 0),
    decision_json TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (deployment_id) REFERENCES canary_deployments(deployment_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (window_id) REFERENCES canary_observation_windows(window_id)
        ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_canary_pointer_current
    ON canary_runtime_pointer_events(sequence, deployment_id, event_type);
CREATE INDEX IF NOT EXISTS idx_canary_assignments_cohort
    ON canary_run_assignments(deployment_id, cohort, stage_pct, risk_level);
CREATE INDEX IF NOT EXISTS idx_canary_decisions_action
    ON canary_stage_decisions(deployment_id, action, created_at);

CREATE TRIGGER IF NOT EXISTS trg_canary_deployment_immutable
BEFORE UPDATE ON canary_deployments BEGIN
    SELECT RAISE(ABORT, 'canary deployments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_canary_deployment_delete_immutable
BEFORE DELETE ON canary_deployments BEGIN
    SELECT RAISE(ABORT, 'canary deployments cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_canary_pointer_immutable
BEFORE UPDATE ON canary_runtime_pointer_events BEGIN
    SELECT RAISE(ABORT, 'canary pointer events are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_canary_pointer_delete_immutable
BEFORE DELETE ON canary_runtime_pointer_events BEGIN
    SELECT RAISE(ABORT, 'canary pointer events cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_canary_assignment_immutable
BEFORE UPDATE ON canary_run_assignments BEGIN
    SELECT RAISE(ABORT, 'canary assignments are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_canary_assignment_delete_immutable
BEFORE DELETE ON canary_run_assignments BEGIN
    SELECT RAISE(ABORT, 'canary assignments cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_canary_observation_immutable
BEFORE UPDATE ON canary_observation_windows BEGIN
    SELECT RAISE(ABORT, 'canary observations are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_canary_observation_delete_immutable
BEFORE DELETE ON canary_observation_windows BEGIN
    SELECT RAISE(ABORT, 'canary observations cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_canary_decision_immutable
BEFORE UPDATE ON canary_stage_decisions BEGIN
    SELECT RAISE(ABORT, 'canary decisions are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_canary_decision_delete_immutable
BEFORE DELETE ON canary_stage_decisions BEGIN
    SELECT RAISE(ABORT, 'canary decisions cannot be deleted');
END;
