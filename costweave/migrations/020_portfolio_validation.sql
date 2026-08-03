CREATE TABLE IF NOT EXISTS portfolio_oracle_results (
    oracle_id TEXT PRIMARY KEY,
    problem_id TEXT NOT NULL,
    comparison_group_hash TEXT NOT NULL,
    topology_kind TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('optimal', 'infeasible_proven', 'skipped_bounds')),
    feasible INTEGER CHECK (feasible IN (0, 1) OR feasible IS NULL),
    optimality_proven INTEGER NOT NULL,
    directional_evidence_eligible INTEGER NOT NULL,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL
        CHECK (may_change_actual_route = 0),
    commercial_claims_allowed INTEGER NOT NULL
        CHECK (commercial_claims_allowed = 0),
    result_json TEXT NOT NULL,
    result_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_portfolio_oracle_problem
    ON portfolio_oracle_results(problem_id, status, optimality_proven, created_at);

CREATE TABLE IF NOT EXISTS portfolio_differential_reports (
    report_id TEXT PRIMARY KEY,
    problem_id TEXT NOT NULL,
    candidate_audit_id TEXT NOT NULL,
    oracle_id TEXT NOT NULL,
    validation_state TEXT NOT NULL,
    exact_validation_passed INTEGER NOT NULL,
    directional_evidence_eligible INTEGER NOT NULL,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL
        CHECK (may_change_actual_route = 0),
    commercial_claims_allowed INTEGER NOT NULL
        CHECK (commercial_claims_allowed = 0),
    report_json TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_audit_id)
        REFERENCES portfolio_audit_reports(audit_id) ON DELETE RESTRICT,
    FOREIGN KEY (oracle_id)
        REFERENCES portfolio_oracle_results(oracle_id) ON DELETE RESTRICT,
    UNIQUE (candidate_audit_id, oracle_id)
);

CREATE INDEX IF NOT EXISTS idx_portfolio_diff_state
    ON portfolio_differential_reports(
        validation_state, exact_validation_passed, created_at
    );

CREATE TABLE IF NOT EXISTS portfolio_sensitivity_reports (
    sensitivity_id TEXT PRIMARY KEY,
    problem_id TEXT NOT NULL,
    baseline_oracle_id TEXT NOT NULL,
    robustness_grade TEXT NOT NULL,
    tested_point_count INTEGER NOT NULL CHECK (tested_point_count > 0),
    directional_evidence_eligible INTEGER NOT NULL,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL
        CHECK (may_change_actual_route = 0),
    commercial_claims_allowed INTEGER NOT NULL
        CHECK (commercial_claims_allowed = 0),
    report_json TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (baseline_oracle_id)
        REFERENCES portfolio_oracle_results(oracle_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_portfolio_sensitivity_grade
    ON portfolio_sensitivity_reports(robustness_grade, created_at);

CREATE TRIGGER IF NOT EXISTS trg_portfolio_oracle_no_authority_update
BEFORE UPDATE OF advisory_only, routing_authority,
    may_change_actual_route, commercial_claims_allowed
ON portfolio_oracle_results
WHEN NEW.advisory_only <> 1
    OR NEW.routing_authority <> 0
    OR NEW.may_change_actual_route <> 0
    OR NEW.commercial_claims_allowed <> 0
BEGIN
    SELECT RAISE(ABORT, 'portfolio oracle cannot control routing or grant claims');
END;

CREATE TRIGGER IF NOT EXISTS trg_portfolio_diff_no_authority_update
BEFORE UPDATE OF advisory_only, routing_authority,
    may_change_actual_route, commercial_claims_allowed
ON portfolio_differential_reports
WHEN NEW.advisory_only <> 1
    OR NEW.routing_authority <> 0
    OR NEW.may_change_actual_route <> 0
    OR NEW.commercial_claims_allowed <> 0
BEGIN
    SELECT RAISE(ABORT, 'portfolio differential cannot control routing');
END;

CREATE TRIGGER IF NOT EXISTS trg_portfolio_sensitivity_no_authority_update
BEFORE UPDATE OF advisory_only, routing_authority,
    may_change_actual_route, commercial_claims_allowed
ON portfolio_sensitivity_reports
WHEN NEW.advisory_only <> 1
    OR NEW.routing_authority <> 0
    OR NEW.may_change_actual_route <> 0
    OR NEW.commercial_claims_allowed <> 0
BEGIN
    SELECT RAISE(ABORT, 'portfolio sensitivity cannot control routing');
END;
