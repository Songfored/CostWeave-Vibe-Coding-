CREATE TABLE IF NOT EXISTS outcome_datasets (
    dataset_id TEXT PRIMARY KEY,
    schema_version TEXT NOT NULL,
    builder_revision TEXT NOT NULL,
    source_report_id TEXT NOT NULL,
    source_report_hash TEXT NOT NULL,
    purpose TEXT NOT NULL,
    sample_count INTEGER NOT NULL,
    effective_task_count REAL NOT NULL,
    content_hash TEXT NOT NULL,
    governance_passed INTEGER NOT NULL,
    training_allowed INTEGER NOT NULL,
    manifest_json TEXT NOT NULL,
    quality_report_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (source_report_hash, purpose, schema_version)
);

CREATE TABLE IF NOT EXISTS outcome_learning_samples (
    sample_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    source_observation_hash TEXT NOT NULL,
    anonymous_task_id TEXT NOT NULL,
    anonymous_project_id TEXT NOT NULL,
    anonymous_user_id TEXT NOT NULL,
    partition_name TEXT NOT NULL,
    dedupe_group TEXT NOT NULL,
    sample_weight REAL NOT NULL,
    evidence_level TEXT NOT NULL,
    purpose TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    sample_json TEXT NOT NULL,
    sample_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (dataset_id)
        REFERENCES outcome_datasets(dataset_id) ON DELETE CASCADE,
    UNIQUE (dataset_id, source_observation_hash)
);

CREATE INDEX IF NOT EXISTS idx_outcome_learning_partition
    ON outcome_learning_samples(
        dataset_id,
        partition_name,
        anonymous_project_id
    );

CREATE INDEX IF NOT EXISTS idx_outcome_learning_ttl
    ON outcome_learning_samples(expires_at, dataset_id);

CREATE TABLE IF NOT EXISTS outcome_label_events (
    label_event_id TEXT PRIMARY KEY,
    sample_id TEXT NOT NULL,
    source TEXT NOT NULL,
    verdict TEXT NOT NULL,
    independent INTEGER NOT NULL,
    blinded INTEGER NOT NULL,
    delayed INTEGER NOT NULL,
    evidence_level TEXT NOT NULL,
    evaluator_revision TEXT NOT NULL,
    event_json TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (sample_id)
        REFERENCES outcome_learning_samples(sample_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_outcome_labels_sample
    ON outcome_label_events(sample_id, recorded_at, label_event_id);

CREATE TABLE IF NOT EXISTS outcome_deletion_audits (
    deletion_id TEXT PRIMARY KEY,
    dataset_id TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    sample_hash TEXT NOT NULL,
    label_set_hash TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    deleted_at TEXT NOT NULL,
    FOREIGN KEY (dataset_id)
        REFERENCES outcome_datasets(dataset_id) ON DELETE CASCADE,
    UNIQUE (dataset_id, sample_id)
);

CREATE INDEX IF NOT EXISTS idx_outcome_deletions_dataset
    ON outcome_deletion_audits(dataset_id, deleted_at, deletion_id);
