CREATE TABLE IF NOT EXISTS policy_revisions (
    revision_id TEXT PRIMARY KEY,
    policy_kind TEXT NOT NULL,
    config_json TEXT NOT NULL,
    checksum TEXT NOT NULL,
    active INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS placements (
    placement_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    policy_revision TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_revision TEXT NOT NULL,
    provider TEXT NOT NULL,
    resource_pool TEXT NOT NULL,
    inference_profile_json TEXT NOT NULL,
    plan_revision INTEGER NOT NULL,
    status TEXT NOT NULL,
    rank INTEGER NOT NULL,
    predicted_quality_lower REAL NOT NULL,
    predicted_start_p50_ms INTEGER NOT NULL,
    predicted_finish_p95_ms INTEGER NOT NULL,
    forecast_cost_json TEXT NOT NULL,
    expected_total_cost REAL NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (run_id, placement_id),
    UNIQUE (run_id, task_id, plan_revision, rank),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, task_id)
        REFERENCES tasks(run_id, task_id) ON DELETE CASCADE,
    FOREIGN KEY (resource_pool)
        REFERENCES resource_pools(pool_id),
    FOREIGN KEY (policy_revision)
        REFERENCES policy_revisions(revision_id)
);

CREATE INDEX IF NOT EXISTS idx_placements_run_task
    ON placements(run_id, task_id, status, rank);

CREATE TABLE IF NOT EXISTS cost_ledger (
    entry_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    placement_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    category TEXT NOT NULL,
    amount REAL NOT NULL,
    attribution_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, task_id)
        REFERENCES tasks(run_id, task_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, placement_id)
        REFERENCES placements(run_id, placement_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cost_ledger_run_phase
    ON cost_ledger(run_id, phase, category);

CREATE TABLE IF NOT EXISTS cache_metadata (
    cache_key TEXT PRIMARY KEY,
    model_revision TEXT NOT NULL,
    prompt_revision TEXT NOT NULL,
    toolchain_revision TEXT NOT NULL,
    permission_scope TEXT NOT NULL,
    input_artifact_hash TEXT NOT NULL,
    freshness_epoch TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);
