CREATE TABLE IF NOT EXISTS capability_nodes_v062 (
    project_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL CHECK(node_type IN (
        'TaskFamily','ModelRevision','ReasoningTier','Tool','Modality',
        'ProviderDeployment','Validator'
    )),
    node_json TEXT NOT NULL CHECK(json_valid(node_json)),
    node_hash TEXT NOT NULL CHECK(length(node_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, node_id)
);

CREATE INDEX IF NOT EXISTS idx_capability_nodes_v062_project_type
ON capability_nodes_v062(project_id, node_type, node_id);

CREATE TRIGGER IF NOT EXISTS capability_nodes_v062_no_update
BEFORE UPDATE ON capability_nodes_v062
BEGIN SELECT RAISE(ABORT, 'capability graph nodes are immutable'); END;

CREATE TRIGGER IF NOT EXISTS capability_nodes_v062_no_delete
BEFORE DELETE ON capability_nodes_v062
BEGIN SELECT RAISE(ABORT, 'capability graph nodes are immutable'); END;

CREATE TABLE IF NOT EXISTS capability_evidence_v062 (
    project_id TEXT NOT NULL,
    evidence_id TEXT NOT NULL,
    condition_key TEXT NOT NULL,
    evidence_json TEXT NOT NULL CHECK(json_valid(evidence_json)),
    evidence_hash TEXT NOT NULL CHECK(length(evidence_hash)=64),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(project_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_capability_evidence_v062_project_condition
ON capability_evidence_v062(project_id, condition_key, recorded_at DESC, evidence_id DESC);

CREATE TRIGGER IF NOT EXISTS capability_evidence_v062_no_update
BEFORE UPDATE ON capability_evidence_v062
BEGIN SELECT RAISE(ABORT, 'capability evidence is immutable'); END;

CREATE TRIGGER IF NOT EXISTS capability_evidence_v062_no_delete
BEFORE DELETE ON capability_evidence_v062
BEGIN SELECT RAISE(ABORT, 'capability evidence is immutable'); END;

CREATE TABLE IF NOT EXISTS capability_evidence_idempotency_v062 (
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 200),
    request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
    evidence_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, idempotency_key),
    UNIQUE(project_id, evidence_id),
    FOREIGN KEY(project_id, evidence_id)
        REFERENCES capability_evidence_v062(project_id, evidence_id)
);

CREATE INDEX IF NOT EXISTS idx_capability_evidence_idempotency_v062_evidence
ON capability_evidence_idempotency_v062(project_id, evidence_id);

CREATE TRIGGER IF NOT EXISTS capability_evidence_idempotency_v062_no_update
BEFORE UPDATE ON capability_evidence_idempotency_v062
BEGIN SELECT RAISE(ABORT, 'capability evidence idempotency is immutable'); END;

CREATE TRIGGER IF NOT EXISTS capability_evidence_idempotency_v062_no_delete
BEFORE DELETE ON capability_evidence_idempotency_v062
BEGIN SELECT RAISE(ABORT, 'capability evidence idempotency is immutable'); END;

CREATE TABLE IF NOT EXISTS capability_calibrations_v062 (
    project_id TEXT NOT NULL,
    posterior_id TEXT NOT NULL,
    condition_key TEXT NOT NULL,
    posterior_json TEXT NOT NULL CHECK(json_valid(posterior_json)),
    posterior_hash TEXT NOT NULL CHECK(length(posterior_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, posterior_id)
);

CREATE INDEX IF NOT EXISTS idx_capability_calibrations_v062_condition
ON capability_calibrations_v062(project_id, condition_key, created_at DESC, posterior_id DESC);

CREATE TRIGGER IF NOT EXISTS capability_calibrations_v062_no_update
BEFORE UPDATE ON capability_calibrations_v062
BEGIN SELECT RAISE(ABORT, 'capability calibrations are immutable'); END;

CREATE TRIGGER IF NOT EXISTS capability_calibrations_v062_no_delete
BEFORE DELETE ON capability_calibrations_v062
BEGIN SELECT RAISE(ABORT, 'capability calibrations are immutable'); END;

CREATE TABLE IF NOT EXISTS capability_route_decisions_v062 (
    project_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    decision_json TEXT NOT NULL CHECK(json_valid(decision_json)),
    decision_hash TEXT NOT NULL CHECK(length(decision_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_capability_route_decisions_v062_project_created
ON capability_route_decisions_v062(project_id, created_at DESC, decision_id DESC);

CREATE TRIGGER IF NOT EXISTS capability_route_decisions_v062_no_update
BEFORE UPDATE ON capability_route_decisions_v062
BEGIN SELECT RAISE(ABORT, 'capability route decisions are immutable'); END;

CREATE TRIGGER IF NOT EXISTS capability_route_decisions_v062_no_delete
BEFORE DELETE ON capability_route_decisions_v062
BEGIN SELECT RAISE(ABORT, 'capability route decisions are immutable'); END;

CREATE TABLE IF NOT EXISTS capability_route_idempotency_v062 (
    project_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL CHECK(length(idempotency_key) BETWEEN 8 AND 200),
    request_hash TEXT NOT NULL CHECK(length(request_hash)=64),
    decision_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, idempotency_key),
    UNIQUE(project_id, decision_id),
    FOREIGN KEY(project_id, decision_id)
        REFERENCES capability_route_decisions_v062(project_id, decision_id)
);

CREATE INDEX IF NOT EXISTS idx_capability_route_idempotency_v062_decision
ON capability_route_idempotency_v062(project_id, decision_id);

CREATE TRIGGER IF NOT EXISTS capability_route_idempotency_v062_no_update
BEFORE UPDATE ON capability_route_idempotency_v062
BEGIN SELECT RAISE(ABORT, 'capability route idempotency is immutable'); END;

CREATE TRIGGER IF NOT EXISTS capability_route_idempotency_v062_no_delete
BEFORE DELETE ON capability_route_idempotency_v062
BEGIN SELECT RAISE(ABORT, 'capability route idempotency is immutable'); END;
