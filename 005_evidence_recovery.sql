CREATE TABLE IF NOT EXISTS validation_results (
    validation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    contract_revision TEXT NOT NULL,
    level TEXT NOT NULL,
    passed INTEGER NOT NULL,
    score REAL NOT NULL,
    false_positive_risk REAL NOT NULL,
    false_negative_risk REAL NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, task_id, attempt),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, task_id)
        REFERENCES tasks(run_id, task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS criterion_results (
    validation_id TEXT NOT NULL,
    criterion_id TEXT NOT NULL,
    criterion_text TEXT NOT NULL,
    critical INTEGER NOT NULL,
    passed INTEGER NOT NULL,
    evidence_json TEXT NOT NULL,
    independent_evidence INTEGER NOT NULL,
    reason TEXT NOT NULL,
    PRIMARY KEY (validation_id, criterion_id),
    FOREIGN KEY (validation_id)
        REFERENCES validation_results(validation_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS failure_records (
    failure_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    classifier_revision TEXT NOT NULL,
    classification TEXT NOT NULL,
    confidence REAL NOT NULL,
    signals_json TEXT NOT NULL,
    source TEXT NOT NULL,
    recovery_action TEXT NOT NULL,
    recovery_reason TEXT NOT NULL,
    affected_tasks_json TEXT NOT NULL,
    estimated_recovery_cost REAL NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, task_id)
        REFERENCES tasks(run_id, task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_failure_records_run_class
    ON failure_records(run_id, classification, created_at);

CREATE TABLE IF NOT EXISTS plan_revisions (
    run_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    patch_id TEXT NOT NULL,
    failure_id TEXT NOT NULL,
    scope TEXT NOT NULL,
    patch_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, plan_revision),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (failure_id)
        REFERENCES failure_records(failure_id)
);
