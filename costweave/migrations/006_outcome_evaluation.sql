CREATE TABLE IF NOT EXISTS forecast_snapshots (
    forecast_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    policy_revision TEXT NOT NULL,
    calibration_revision TEXT NOT NULL,
    seed INTEGER NOT NULL,
    sample_size INTEGER NOT NULL,
    forecast_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS outcomes (
    outcome_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    category TEXT NOT NULL,
    success INTEGER NOT NULL,
    quality REAL NOT NULL,
    cost REAL NOT NULL,
    completion_seconds REAL NOT NULL,
    critical_false_pass INTEGER NOT NULL,
    attribution_json TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS forecast_calibrators (
    calibrator_id TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    scope_key TEXT NOT NULL,
    revision TEXT NOT NULL,
    parameters_json TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    active INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (level, scope_key, revision)
);

CREATE TABLE IF NOT EXISTS capability_posteriors (
    scope_key TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    successes REAL NOT NULL,
    failures REAL NOT NULL,
    sample_size REAL NOT NULL,
    revision TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evaluation_runs (
    evaluation_id TEXT PRIMARY KEY,
    revision TEXT NOT NULL,
    sample_size INTEGER NOT NULL,
    result_json TEXT NOT NULL,
    passed INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcomes_category
    ON outcomes(category, recorded_at);

