CREATE TABLE IF NOT EXISTS portfolio_audit_reports (
    audit_id TEXT PRIMARY KEY,
    problem_id TEXT NOT NULL UNIQUE,
    comparison_group_hash TEXT NOT NULL,
    topology_kind TEXT NOT NULL
        CHECK (topology_kind IN ('current', 'fine_grained', 'consolidated')),
    feasible INTEGER NOT NULL,
    directional_evidence_eligible INTEGER NOT NULL,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL
        CHECK (may_change_actual_route = 0),
    commercial_claims_allowed INTEGER NOT NULL
        CHECK (commercial_claims_allowed = 0),
    report_json TEXT NOT NULL,
    report_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_portfolio_audit_group
    ON portfolio_audit_reports(
        comparison_group_hash, topology_kind, feasible, created_at
    );

CREATE TABLE IF NOT EXISTS portfolio_scenario_comparisons (
    comparison_id TEXT PRIMARY KEY,
    comparison_group_hash TEXT NOT NULL UNIQUE,
    preferred_audit_id TEXT,
    all_feasible INTEGER NOT NULL,
    directional_evidence_eligible INTEGER NOT NULL,
    advisory_only INTEGER NOT NULL CHECK (advisory_only = 1),
    routing_authority INTEGER NOT NULL CHECK (routing_authority = 0),
    may_change_actual_route INTEGER NOT NULL
        CHECK (may_change_actual_route = 0),
    commercial_claims_allowed INTEGER NOT NULL
        CHECK (commercial_claims_allowed = 0),
    comparison_json TEXT NOT NULL,
    comparison_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (preferred_audit_id)
        REFERENCES portfolio_audit_reports(audit_id) ON DELETE RESTRICT
);

CREATE TABLE IF NOT EXISTS portfolio_comparison_members (
    comparison_id TEXT NOT NULL,
    audit_id TEXT NOT NULL,
    member_rank INTEGER NOT NULL CHECK (member_rank >= 0),
    PRIMARY KEY (comparison_id, audit_id),
    UNIQUE (comparison_id, member_rank),
    FOREIGN KEY (comparison_id)
        REFERENCES portfolio_scenario_comparisons(comparison_id)
        ON DELETE RESTRICT,
    FOREIGN KEY (audit_id)
        REFERENCES portfolio_audit_reports(audit_id) ON DELETE RESTRICT
);

CREATE TRIGGER IF NOT EXISTS trg_portfolio_audit_no_authority_update
BEFORE UPDATE OF advisory_only, routing_authority,
    may_change_actual_route, commercial_claims_allowed
ON portfolio_audit_reports
WHEN NEW.advisory_only <> 1
    OR NEW.routing_authority <> 0
    OR NEW.may_change_actual_route <> 0
    OR NEW.commercial_claims_allowed <> 0
BEGIN
    SELECT RAISE(ABORT, 'portfolio audit cannot control routing or grant claims');
END;

CREATE TRIGGER IF NOT EXISTS trg_portfolio_compare_no_authority_update
BEFORE UPDATE OF advisory_only, routing_authority,
    may_change_actual_route, commercial_claims_allowed
ON portfolio_scenario_comparisons
WHEN NEW.advisory_only <> 1
    OR NEW.routing_authority <> 0
    OR NEW.may_change_actual_route <> 0
    OR NEW.commercial_claims_allowed <> 0
BEGIN
    SELECT RAISE(ABORT, 'portfolio comparison cannot control routing or grant claims');
END;
