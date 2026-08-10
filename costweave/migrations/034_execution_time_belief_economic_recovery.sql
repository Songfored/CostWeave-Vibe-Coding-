CREATE TABLE IF NOT EXISTS execution_beliefs_v064 (
    project_id TEXT NOT NULL,
    belief_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    belief_json TEXT NOT NULL CHECK(json_valid(belief_json)),
    belief_hash TEXT NOT NULL CHECK(length(belief_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, belief_id)
);

CREATE INDEX IF NOT EXISTS idx_execution_beliefs_v064_project_case
ON execution_beliefs_v064(project_id, case_id, created_at DESC, belief_id DESC);

CREATE TRIGGER IF NOT EXISTS execution_beliefs_v064_no_update
BEFORE UPDATE ON execution_beliefs_v064
BEGIN SELECT RAISE(ABORT, 'execution beliefs are immutable'); END;

CREATE TRIGGER IF NOT EXISTS execution_beliefs_v064_no_delete
BEFORE DELETE ON execution_beliefs_v064
BEGIN SELECT RAISE(ABORT, 'execution beliefs are immutable'); END;

CREATE TABLE IF NOT EXISTS execution_recovery_decisions_v064 (
    project_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN (
        'CONTINUE', 'RETRY_NODE', 'SWITCH_MODEL', 'REQUEST_MISSING_INFO',
        'REPLAN_SUBGRAPH', 'FULL_REPLAN', 'STOP_AND_RETURN_PARTIAL'
    )),
    decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
    decision_hash TEXT NOT NULL CHECK(length(decision_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_execution_recovery_decisions_v064_project_created
ON execution_recovery_decisions_v064(project_id, created_at DESC, decision_id DESC);

CREATE TRIGGER IF NOT EXISTS execution_recovery_decisions_v064_no_update
BEFORE UPDATE ON execution_recovery_decisions_v064
BEGIN SELECT RAISE(ABORT, 'execution recovery decisions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS execution_recovery_decisions_v064_no_delete
BEFORE DELETE ON execution_recovery_decisions_v064
BEGIN SELECT RAISE(ABORT, 'execution recovery decisions are immutable'); END;

CREATE TABLE IF NOT EXISTS execution_recovery_idempotency_v064 (
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 200),
    request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
    decision_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, idempotency_key),
    UNIQUE(project_id, decision_id),
    FOREIGN KEY(project_id, decision_id)
        REFERENCES execution_recovery_decisions_v064(project_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_execution_recovery_idempotency_v064_decision
ON execution_recovery_idempotency_v064(project_id, decision_id);

CREATE TRIGGER IF NOT EXISTS execution_recovery_idempotency_v064_no_update
BEFORE UPDATE ON execution_recovery_idempotency_v064
BEGIN SELECT RAISE(ABORT, 'execution recovery idempotency records are immutable'); END;

CREATE TRIGGER IF NOT EXISTS execution_recovery_idempotency_v064_no_delete
BEFORE DELETE ON execution_recovery_idempotency_v064
BEGIN SELECT RAISE(ABORT, 'execution recovery idempotency records are immutable'); END;
