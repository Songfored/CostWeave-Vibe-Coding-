CREATE TABLE IF NOT EXISTS safe_learning_credits_v065 (
    project_id TEXT NOT NULL,
    credit_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    credit_json TEXT NOT NULL CHECK(json_valid(credit_json)),
    credit_hash TEXT NOT NULL CHECK(length(credit_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, credit_id)
);

CREATE INDEX IF NOT EXISTS idx_safe_learning_credits_v065_project_created
ON safe_learning_credits_v065(project_id, created_at DESC, credit_id DESC);

CREATE TRIGGER IF NOT EXISTS safe_learning_credits_v065_no_update
BEFORE UPDATE ON safe_learning_credits_v065
BEGIN SELECT RAISE(ABORT, 'safe learning credits are immutable'); END;

CREATE TRIGGER IF NOT EXISTS safe_learning_credits_v065_no_delete
BEFORE DELETE ON safe_learning_credits_v065
BEGIN SELECT RAISE(ABORT, 'safe learning credits are immutable'); END;

CREATE TABLE IF NOT EXISTS safe_learning_replays_v065 (
    project_id TEXT NOT NULL,
    replay_id TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    replay_json TEXT NOT NULL CHECK(json_valid(replay_json)),
    replay_hash TEXT NOT NULL CHECK(length(replay_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, replay_id)
);

CREATE INDEX IF NOT EXISTS idx_safe_learning_replays_v065_project_created
ON safe_learning_replays_v065(project_id, created_at DESC, replay_id DESC);

CREATE TRIGGER IF NOT EXISTS safe_learning_replays_v065_no_update
BEFORE UPDATE ON safe_learning_replays_v065
BEGIN SELECT RAISE(ABORT, 'safe learning replays are immutable'); END;

CREATE TRIGGER IF NOT EXISTS safe_learning_replays_v065_no_delete
BEFORE DELETE ON safe_learning_replays_v065
BEGIN SELECT RAISE(ABORT, 'safe learning replays are immutable'); END;

CREATE TABLE IF NOT EXISTS safe_learning_estimates_v065 (
    project_id TEXT NOT NULL,
    estimate_id TEXT NOT NULL,
    task_family TEXT NOT NULL,
    estimate_json TEXT NOT NULL CHECK(json_valid(estimate_json)),
    estimate_hash TEXT NOT NULL CHECK(length(estimate_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, estimate_id)
);

CREATE INDEX IF NOT EXISTS idx_safe_learning_estimates_v065_project_created
ON safe_learning_estimates_v065(project_id, created_at DESC, estimate_id DESC);

CREATE TRIGGER IF NOT EXISTS safe_learning_estimates_v065_no_update
BEFORE UPDATE ON safe_learning_estimates_v065
BEGIN SELECT RAISE(ABORT, 'safe learning estimates are immutable'); END;

CREATE TRIGGER IF NOT EXISTS safe_learning_estimates_v065_no_delete
BEFORE DELETE ON safe_learning_estimates_v065
BEGIN SELECT RAISE(ABORT, 'safe learning estimates are immutable'); END;

CREATE TABLE IF NOT EXISTS safe_learning_candidates_v065 (
    project_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    task_family TEXT NOT NULL,
    candidate_json TEXT NOT NULL CHECK(json_valid(candidate_json)),
    candidate_hash TEXT NOT NULL CHECK(length(candidate_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, candidate_id)
);

CREATE INDEX IF NOT EXISTS idx_safe_learning_candidates_v065_project_created
ON safe_learning_candidates_v065(project_id, created_at DESC, candidate_id DESC);

CREATE TRIGGER IF NOT EXISTS safe_learning_candidates_v065_no_update
BEFORE UPDATE ON safe_learning_candidates_v065
BEGIN SELECT RAISE(ABORT, 'safe learning candidates are immutable'); END;

CREATE TRIGGER IF NOT EXISTS safe_learning_candidates_v065_no_delete
BEFORE DELETE ON safe_learning_candidates_v065
BEGIN SELECT RAISE(ABORT, 'safe learning candidates are immutable'); END;

CREATE TABLE IF NOT EXISTS safe_learning_assessments_v065 (
    project_id TEXT NOT NULL,
    assessment_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    stage TEXT NOT NULL CHECK(stage IN ('offline','shadow','canary')),
    assessment_json TEXT NOT NULL CHECK(json_valid(assessment_json)),
    assessment_hash TEXT NOT NULL CHECK(length(assessment_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, assessment_id)
);

CREATE INDEX IF NOT EXISTS idx_safe_learning_assessments_v065_project_created
ON safe_learning_assessments_v065(project_id, created_at DESC, assessment_id DESC);

CREATE TRIGGER IF NOT EXISTS safe_learning_assessments_v065_no_update
BEFORE UPDATE ON safe_learning_assessments_v065
BEGIN SELECT RAISE(ABORT, 'safe learning assessments are immutable'); END;

CREATE TRIGGER IF NOT EXISTS safe_learning_assessments_v065_no_delete
BEFORE DELETE ON safe_learning_assessments_v065
BEGIN SELECT RAISE(ABORT, 'safe learning assessments are immutable'); END;

CREATE TABLE IF NOT EXISTS safe_learning_idempotency_v065 (
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 200),
    request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
    object_kind TEXT NOT NULL CHECK(object_kind IN ('credit','replay','estimate','candidate','assessment')),
    object_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, idempotency_key),
    UNIQUE(project_id, object_kind, object_id)
);

CREATE INDEX IF NOT EXISTS idx_safe_learning_idempotency_v065_object
ON safe_learning_idempotency_v065(project_id, object_kind, object_id);

CREATE TRIGGER IF NOT EXISTS safe_learning_idempotency_v065_no_update
BEFORE UPDATE ON safe_learning_idempotency_v065
BEGIN SELECT RAISE(ABORT, 'safe learning idempotency records are immutable'); END;

CREATE TRIGGER IF NOT EXISTS safe_learning_idempotency_v065_no_delete
BEFORE DELETE ON safe_learning_idempotency_v065
BEGIN SELECT RAISE(ABORT, 'safe learning idempotency records are immutable'); END;
