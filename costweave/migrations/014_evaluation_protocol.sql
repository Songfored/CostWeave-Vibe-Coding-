CREATE TABLE IF NOT EXISTS evaluation_observations (
    observation_id TEXT PRIMARY KEY,
    evaluation_id TEXT NOT NULL,
    experiment_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    project_id TEXT NOT NULL,
    partition_name TEXT NOT NULL,
    baseline_id TEXT NOT NULL,
    blind_alias TEXT NOT NULL,
    evidence_level TEXT NOT NULL,
    provider_calls INTEGER NOT NULL,
    observation_json TEXT NOT NULL,
    observation_hash TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (evaluation_id)
        REFERENCES evaluation_runs(evaluation_id) ON DELETE CASCADE,
    UNIQUE (evaluation_id, task_id, baseline_id),
    UNIQUE (evaluation_id, blind_alias)
);

CREATE INDEX IF NOT EXISTS idx_evaluation_observations_matrix
    ON evaluation_observations(
        evaluation_id,
        partition_name,
        task_id,
        baseline_id
    );

CREATE INDEX IF NOT EXISTS idx_evaluation_observations_experiment
    ON evaluation_observations(experiment_id, recorded_at);
