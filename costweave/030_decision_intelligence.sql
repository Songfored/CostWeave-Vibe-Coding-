CREATE TABLE IF NOT EXISTS decision_objects_v060 (
    project_id TEXT NOT NULL,
    object_id TEXT NOT NULL,
    object_kind TEXT NOT NULL CHECK(object_kind IN (
        'task_belief','system_belief','capability_posterior',
        'strategy_candidate','decision_trace','outcome_credit'
    )),
    schema_version TEXT NOT NULL CHECK(schema_version='v0.6.0'),
    payload_json TEXT NOT NULL CHECK(json_valid(payload_json)),
    payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64),
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(project_id, object_id)
);

CREATE INDEX IF NOT EXISTS idx_decision_objects_v060_project_kind_recorded
ON decision_objects_v060(project_id, object_kind, recorded_at DESC);

CREATE INDEX IF NOT EXISTS idx_decision_objects_v060_project_recorded
ON decision_objects_v060(project_id, recorded_at DESC);

CREATE TRIGGER IF NOT EXISTS decision_objects_v060_no_update
BEFORE UPDATE ON decision_objects_v060
BEGIN SELECT RAISE(ABORT, 'decision objects are immutable'); END;

CREATE TRIGGER IF NOT EXISTS decision_objects_v060_no_delete
BEFORE DELETE ON decision_objects_v060
BEGIN SELECT RAISE(ABORT, 'decision objects are immutable'); END;

CREATE TABLE IF NOT EXISTS tournament_runs_v060 (
    project_id TEXT NOT NULL,
    tournament_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    dataset_id TEXT NOT NULL,
    manifest_hash TEXT NOT NULL CHECK(length(manifest_hash)=64),
    report_json TEXT NOT NULL CHECK(json_valid(report_json)),
    report_hash TEXT NOT NULL CHECK(length(report_hash)=64),
    created_at TEXT NOT NULL,
    PRIMARY KEY(project_id, tournament_id),
    UNIQUE(project_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_tournament_runs_v060_project_created
ON tournament_runs_v060(project_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS tournament_runs_v060_no_update
BEFORE UPDATE ON tournament_runs_v060
BEGIN SELECT RAISE(ABORT, 'tournament runs are immutable'); END;

CREATE TRIGGER IF NOT EXISTS tournament_runs_v060_no_delete
BEFORE DELETE ON tournament_runs_v060
BEGIN SELECT RAISE(ABORT, 'tournament runs are immutable'); END;
