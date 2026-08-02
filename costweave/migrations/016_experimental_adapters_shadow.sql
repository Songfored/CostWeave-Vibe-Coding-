CREATE TABLE IF NOT EXISTS external_experiments (
    experiment_id TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    status TEXT NOT NULL,
    purpose TEXT NOT NULL,
    total_cost_limit REAL NOT NULL,
    provider_limits_json TEXT NOT NULL,
    max_logical_calls INTEGER NOT NULL,
    max_attempts_per_call INTEGER NOT NULL,
    config_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS external_calls (
    external_call_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    deployment_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    model_id TEXT NOT NULL,
    adapter_revision TEXT NOT NULL,
    reasoning_effort TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    status TEXT NOT NULL,
    reserved_cost REAL NOT NULL,
    actual_cost REAL NOT NULL DEFAULT 0,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    result_json TEXT,
    telemetry_json TEXT,
    error_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id)
        REFERENCES external_experiments(experiment_id) ON DELETE CASCADE,
    UNIQUE (experiment_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_external_calls_budget
    ON external_calls(experiment_id, provider, status, created_at);

CREATE INDEX IF NOT EXISTS idx_external_calls_trace
    ON external_calls(run_id, task_id, deployment_id, created_at);

CREATE TABLE IF NOT EXISTS external_call_attempts (
    attempt_id TEXT PRIMARY KEY,
    external_call_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    status TEXT NOT NULL,
    status_code INTEGER,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    actual_cost REAL NOT NULL,
    request_latency_ms INTEGER NOT NULL,
    queue_latency_ms INTEGER NOT NULL,
    rate_limited INTEGER NOT NULL,
    cancelled INTEGER NOT NULL,
    error_code TEXT,
    response_hash TEXT,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (external_call_id)
        REFERENCES external_calls(external_call_id) ON DELETE CASCADE,
    UNIQUE (external_call_id, ordinal)
);

CREATE TABLE IF NOT EXISTS shadow_decisions (
    shadow_decision_id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    policy_revision TEXT NOT NULL,
    catalog_revision TEXT NOT NULL,
    actual_route_hash TEXT NOT NULL,
    task_fingerprint TEXT NOT NULL,
    recommendation_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    rejections_json TEXT NOT NULL,
    decision_hash TEXT NOT NULL,
    advisory_only INTEGER NOT NULL,
    intervention_count INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (experiment_id)
        REFERENCES external_experiments(experiment_id) ON DELETE CASCADE,
    UNIQUE (experiment_id, task_id, policy_revision)
);

CREATE INDEX IF NOT EXISTS idx_shadow_decisions_trace
    ON shadow_decisions(experiment_id, run_id, created_at);

CREATE TABLE IF NOT EXISTS shadow_comparisons (
    comparison_id TEXT PRIMARY KEY,
    shadow_decision_id TEXT NOT NULL UNIQUE,
    actual_outcome_hash TEXT NOT NULL,
    comparison_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (shadow_decision_id)
        REFERENCES shadow_decisions(shadow_decision_id) ON DELETE CASCADE
);
