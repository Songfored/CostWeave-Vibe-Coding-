CREATE TABLE IF NOT EXISTS meta_decisions_v061 (
    project_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    belief_hash TEXT NOT NULL CHECK(length(belief_hash)=64),
    action TEXT NOT NULL CHECK(action IN ('fast','deliberate','probe','expert')),
    decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
    decision_hash TEXT NOT NULL CHECK(length(decision_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_meta_decisions_v061_project_created
ON meta_decisions_v061(project_id, created_at DESC, decision_id DESC);

CREATE INDEX IF NOT EXISTS idx_meta_decisions_v061_project_action
ON meta_decisions_v061(project_id, action, created_at DESC);

CREATE TRIGGER IF NOT EXISTS meta_decisions_v061_no_update
BEFORE UPDATE ON meta_decisions_v061
BEGIN SELECT RAISE(ABORT, 'meta decisions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS meta_decisions_v061_no_delete
BEFORE DELETE ON meta_decisions_v061
BEGIN SELECT RAISE(ABORT, 'meta decisions are immutable'); END;

CREATE TABLE IF NOT EXISTS meta_decision_idempotency_v061 (
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 200),
    request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
    decision_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, idempotency_key),
    UNIQUE(project_id, decision_id),
    FOREIGN KEY(project_id, decision_id)
        REFERENCES meta_decisions_v061(project_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_meta_decision_idempotency_v061_decision
ON meta_decision_idempotency_v061(project_id, decision_id);

CREATE TRIGGER IF NOT EXISTS meta_decision_idempotency_v061_no_update
BEFORE UPDATE ON meta_decision_idempotency_v061
BEGIN SELECT RAISE(ABORT, 'meta decision idempotency records are immutable'); END;

CREATE TRIGGER IF NOT EXISTS meta_decision_idempotency_v061_no_delete
BEFORE DELETE ON meta_decision_idempotency_v061
BEGIN SELECT RAISE(ABORT, 'meta decision idempotency records are immutable'); END;

CREATE TABLE IF NOT EXISTS planning_budget_accounts_v061 (
    project_id TEXT NOT NULL,
    budget_id TEXT NOT NULL,
    budget_json TEXT NOT NULL CHECK(json_valid(budget_json)),
    budget_hash TEXT NOT NULL CHECK(length(budget_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, budget_id)
);

CREATE INDEX IF NOT EXISTS idx_planning_budget_accounts_v061_project_created
ON planning_budget_accounts_v061(project_id, created_at DESC, budget_id DESC);

CREATE TRIGGER IF NOT EXISTS planning_budget_accounts_v061_no_update
BEFORE UPDATE ON planning_budget_accounts_v061
BEGIN SELECT RAISE(ABORT, 'planning budget accounts are immutable'); END;

CREATE TRIGGER IF NOT EXISTS planning_budget_accounts_v061_no_delete
BEFORE DELETE ON planning_budget_accounts_v061
BEGIN SELECT RAISE(ABORT, 'planning budget accounts are immutable'); END;

CREATE TABLE IF NOT EXISTS planning_budget_events_v061 (
    project_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    budget_id TEXT NOT NULL,
    reservation_id TEXT NOT NULL,
    event_type TEXT NOT NULL CHECK(event_type IN ('reserve','release','commit')),
    event_json TEXT NOT NULL CHECK(json_valid(event_json)),
    event_hash TEXT NOT NULL CHECK(length(event_hash)=64),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(project_id, event_id),
    UNIQUE(project_id, reservation_id, event_type)
);

CREATE INDEX IF NOT EXISTS idx_planning_budget_events_v061_project_budget
ON planning_budget_events_v061(project_id, budget_id, recorded_at DESC, event_id DESC);

CREATE INDEX IF NOT EXISTS idx_planning_budget_events_v061_project_reservation
ON planning_budget_events_v061(project_id, reservation_id, recorded_at ASC, event_id ASC);

CREATE TRIGGER IF NOT EXISTS planning_budget_events_v061_no_update
BEFORE UPDATE ON planning_budget_events_v061
BEGIN SELECT RAISE(ABORT, 'planning budget events are immutable'); END;

CREATE TRIGGER IF NOT EXISTS planning_budget_events_v061_no_delete
BEFORE DELETE ON planning_budget_events_v061
BEGIN SELECT RAISE(ABORT, 'planning budget events are immutable'); END;
