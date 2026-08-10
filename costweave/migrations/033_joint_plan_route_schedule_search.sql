CREATE TABLE IF NOT EXISTS joint_search_candidates_v063 (
    project_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('safety_baseline','historical','planner','local_transform')),
    candidate_json TEXT NOT NULL CHECK(json_valid(candidate_json)),
    candidate_hash TEXT NOT NULL CHECK(length(candidate_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_joint_search_candidates_v063_project_case
ON joint_search_candidates_v063(project_id, case_id, created_at DESC, candidate_id DESC);

CREATE TRIGGER IF NOT EXISTS joint_search_candidates_v063_no_update
BEFORE UPDATE ON joint_search_candidates_v063
BEGIN SELECT RAISE(ABORT, 'joint search candidates are immutable'); END;

CREATE TRIGGER IF NOT EXISTS joint_search_candidates_v063_no_delete
BEFORE DELETE ON joint_search_candidates_v063
BEGIN SELECT RAISE(ABORT, 'joint search candidates are immutable'); END;

CREATE TABLE IF NOT EXISTS joint_search_decisions_v063 (
    project_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    case_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('SELECT','ABSTAIN','ESCALATE')),
    decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
    decision_hash TEXT NOT NULL CHECK(length(decision_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_joint_search_decisions_v063_project_created
ON joint_search_decisions_v063(project_id, created_at DESC, decision_id DESC);

CREATE TRIGGER IF NOT EXISTS joint_search_decisions_v063_no_update
BEFORE UPDATE ON joint_search_decisions_v063
BEGIN SELECT RAISE(ABORT, 'joint search decisions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS joint_search_decisions_v063_no_delete
BEFORE DELETE ON joint_search_decisions_v063
BEGIN SELECT RAISE(ABORT, 'joint search decisions are immutable'); END;

CREATE TABLE IF NOT EXISTS joint_search_idempotency_v063 (
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 200),
    request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
    decision_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, idempotency_key),
    UNIQUE(project_id, decision_id),
    FOREIGN KEY(project_id, decision_id)
        REFERENCES joint_search_decisions_v063(project_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_joint_search_idempotency_v063_decision
ON joint_search_idempotency_v063(project_id, decision_id);

CREATE TRIGGER IF NOT EXISTS joint_search_idempotency_v063_no_update
BEFORE UPDATE ON joint_search_idempotency_v063
BEGIN SELECT RAISE(ABORT, 'joint search idempotency records are immutable'); END;

CREATE TRIGGER IF NOT EXISTS joint_search_idempotency_v063_no_delete
BEFORE DELETE ON joint_search_idempotency_v063
BEGIN SELECT RAISE(ABORT, 'joint search idempotency records are immutable'); END;
