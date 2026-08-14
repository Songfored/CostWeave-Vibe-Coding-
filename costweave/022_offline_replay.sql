CREATE TABLE IF NOT EXISTS replay_version_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    external_calls_allowed INTEGER NOT NULL CHECK (external_calls_allowed = 0),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL CHECK (may_change_actual_route = 0),
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS offline_replay_cases (
    case_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    case_hash TEXT NOT NULL UNIQUE,
    task_family TEXT NOT NULL,
    risk_level TEXT NOT NULL CHECK (risk_level IN ('low','medium','high','critical')),
    multi_route_evaluation_allowed INTEGER NOT NULL,
    case_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES replay_version_snapshots(snapshot_id)
        ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS observed_route_outcomes (
    outcome_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    route_id TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    data_origin TEXT NOT NULL,
    independently_validated INTEGER NOT NULL,
    outcome_json TEXT NOT NULL,
    outcome_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES offline_replay_cases(case_id)
        ON DELETE RESTRICT,
    UNIQUE (case_id, route_id)
);

CREATE TABLE IF NOT EXISTS offline_replay_results (
    replay_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    policy_id TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('replayed','no_feasible_route')),
    external_call_count INTEGER NOT NULL CHECK (external_call_count = 0),
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL CHECK (may_change_actual_route = 0),
    replay_json TEXT NOT NULL,
    replay_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES replay_version_snapshots(snapshot_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (case_id) REFERENCES offline_replay_cases(case_id)
        ON DELETE RESTRICT,
    UNIQUE (case_id, policy_id, policy_version)
);

CREATE TABLE IF NOT EXISTS approximate_oracle_reports (
    oracle_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    outcome_set_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'subset_forbidden','no_observed_routes','no_accepted_route','approximate_oracle'
    )),
    directional_evidence_eligible INTEGER NOT NULL,
    approximate_only INTEGER NOT NULL CHECK (approximate_only = 1),
    global_optimality_proven INTEGER NOT NULL CHECK (global_optimality_proven = 0),
    external_call_count INTEGER NOT NULL CHECK (external_call_count = 0),
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL CHECK (may_change_actual_route = 0),
    commercial_claims_allowed INTEGER NOT NULL CHECK (commercial_claims_allowed = 0),
    oracle_json TEXT NOT NULL,
    oracle_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES replay_version_snapshots(snapshot_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (case_id) REFERENCES offline_replay_cases(case_id)
        ON DELETE RESTRICT,
    UNIQUE (case_id, outcome_set_hash)
);

CREATE TABLE IF NOT EXISTS replay_benchmark_reports (
    report_id TEXT PRIMARY KEY,
    snapshot_id TEXT NOT NULL,
    case_set_hash TEXT NOT NULL,
    policy_set_hash TEXT NOT NULL,
    outcome_set_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN (
        'blocked_evidence','incomplete_counterfactuals','directional_replay'
    )),
    threshold_assessment_allowed INTEGER NOT NULL,
    external_call_count INTEGER NOT NULL CHECK (external_call_count = 0),
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL CHECK (may_change_actual_route = 0),
    policy_promotion_allowed INTEGER NOT NULL CHECK (policy_promotion_allowed = 0),
    commercial_claims_allowed INTEGER NOT NULL CHECK (commercial_claims_allowed = 0),
    report_json TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (snapshot_id) REFERENCES replay_version_snapshots(snapshot_id)
        ON DELETE RESTRICT,
    UNIQUE (snapshot_id, case_set_hash, policy_set_hash, outcome_set_hash)
);

CREATE INDEX IF NOT EXISTS idx_replay_case_snapshot
    ON offline_replay_cases(snapshot_id, task_family, risk_level);
CREATE INDEX IF NOT EXISTS idx_replay_result_policy
    ON offline_replay_results(policy_id, policy_version, state);
CREATE INDEX IF NOT EXISTS idx_replay_oracle_state
    ON approximate_oracle_reports(state, directional_evidence_eligible);
CREATE INDEX IF NOT EXISTS idx_replay_benchmark_state
    ON replay_benchmark_reports(state, threshold_assessment_allowed);

CREATE TRIGGER IF NOT EXISTS trg_replay_snapshot_immutable
BEFORE UPDATE ON replay_version_snapshots BEGIN
    SELECT RAISE(ABORT, 'replay snapshots are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_replay_snapshot_delete_immutable
BEFORE DELETE ON replay_version_snapshots BEGIN
    SELECT RAISE(ABORT, 'replay snapshots cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_replay_case_immutable
BEFORE UPDATE ON offline_replay_cases BEGIN
    SELECT RAISE(ABORT, 'replay cases are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_replay_case_delete_immutable
BEFORE DELETE ON offline_replay_cases BEGIN
    SELECT RAISE(ABORT, 'replay cases cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_route_outcome_immutable
BEFORE UPDATE ON observed_route_outcomes BEGIN
    SELECT RAISE(ABORT, 'observed route outcomes are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_route_outcome_delete_immutable
BEFORE DELETE ON observed_route_outcomes BEGIN
    SELECT RAISE(ABORT, 'observed route outcomes cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_offline_replay_immutable
BEFORE UPDATE ON offline_replay_results BEGIN
    SELECT RAISE(ABORT, 'offline replay results are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_offline_replay_delete_immutable
BEFORE DELETE ON offline_replay_results BEGIN
    SELECT RAISE(ABORT, 'offline replay results cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_approx_oracle_immutable
BEFORE UPDATE ON approximate_oracle_reports BEGIN
    SELECT RAISE(ABORT, 'approximate oracle reports are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_approx_oracle_delete_immutable
BEFORE DELETE ON approximate_oracle_reports BEGIN
    SELECT RAISE(ABORT, 'approximate oracle reports cannot be deleted');
END;
CREATE TRIGGER IF NOT EXISTS trg_replay_benchmark_immutable
BEFORE UPDATE ON replay_benchmark_reports BEGIN
    SELECT RAISE(ABORT, 'replay benchmark reports are immutable');
END;
CREATE TRIGGER IF NOT EXISTS trg_replay_benchmark_delete_immutable
BEFORE DELETE ON replay_benchmark_reports BEGIN
    SELECT RAISE(ABORT, 'replay benchmark reports cannot be deleted');
END;
