CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    producer_task_id TEXT NOT NULL,
    artifact_type TEXT NOT NULL,
    schema_ref TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    sensitivity TEXT NOT NULL,
    producer_model TEXT,
    input_refs_json TEXT NOT NULL,
    content_json TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, producer_task_id)
        REFERENCES tasks(run_id, task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_artifacts_run_task
    ON artifacts(run_id, producer_task_id, created_at);

CREATE TABLE IF NOT EXISTS artifact_links (
    run_id TEXT NOT NULL,
    parent_artifact_id TEXT NOT NULL,
    child_artifact_id TEXT NOT NULL,
    link_kind TEXT NOT NULL,
    PRIMARY KEY (run_id, parent_artifact_id, child_artifact_id, link_kind),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (parent_artifact_id) REFERENCES artifacts(artifact_id),
    FOREIGN KEY (child_artifact_id) REFERENCES artifacts(artifact_id)
);

CREATE TABLE IF NOT EXISTS task_dependencies (
    run_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    dependency_task_id TEXT NOT NULL,
    dependency_kind TEXT NOT NULL,
    required_schema TEXT,
    PRIMARY KEY (
        run_id, plan_revision, task_id, dependency_task_id, dependency_kind
    ),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_task_dependencies_run_plan
    ON task_dependencies(run_id, plan_revision);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    recipient_task_id TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    kind TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    envelope_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (run_id, recipient_task_id, plan_revision, attempt),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, recipient_task_id)
        REFERENCES tasks(run_id, task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_run_task
    ON messages(run_id, recipient_task_id, plan_revision, attempt);
