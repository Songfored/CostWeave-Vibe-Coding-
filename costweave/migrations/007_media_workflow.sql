CREATE TABLE IF NOT EXISTS media_projects (
    project_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL,
    strategy TEXT NOT NULL,
    status TEXT NOT NULL,
    profile_json TEXT NOT NULL,
    project_json TEXT NOT NULL,
    revision TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS media_bible_revisions (
    project_id TEXT NOT NULL,
    bible_type TEXT NOT NULL,
    bible_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    content_json TEXT NOT NULL,
    locked_intent_hash TEXT,
    created_at TEXT NOT NULL,
    PRIMARY KEY (project_id, bible_type, bible_id, revision),
    FOREIGN KEY (project_id)
        REFERENCES media_projects(project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS shot_contracts (
    project_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    status TEXT NOT NULL,
    approval_status TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    locked_intent_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project_id, shot_id),
    FOREIGN KEY (project_id)
        REFERENCES media_projects(project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS prompt_revisions (
    prompt_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    target_adapter TEXT NOT NULL,
    locked_intent_hash TEXT NOT NULL,
    semantic_drift INTEGER NOT NULL,
    prompt_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, shot_id, revision, target_adapter),
    FOREIGN KEY (project_id, shot_id)
        REFERENCES shot_contracts(project_id, shot_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS media_render_attempts (
    attempt_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    stage TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    status TEXT NOT NULL,
    simulated INTEGER NOT NULL,
    artifact_json TEXT NOT NULL,
    cost REAL NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE (project_id, shot_id, stage, attempt),
    FOREIGN KEY (project_id, shot_id)
        REFERENCES shot_contracts(project_id, shot_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS media_continuity_checks (
    check_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    shot_id TEXT NOT NULL,
    passed INTEGER NOT NULL,
    defects_json TEXT NOT NULL,
    suspect_shots_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id, shot_id)
        REFERENCES shot_contracts(project_id, shot_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS media_cost_ledger (
    entry_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    shot_id TEXT,
    category TEXT NOT NULL,
    amount REAL NOT NULL,
    seconds REAL NOT NULL,
    accepted INTEGER NOT NULL,
    attempt INTEGER NOT NULL,
    entry_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id)
        REFERENCES media_projects(project_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_media_render_project_stage
    ON media_render_attempts(project_id, stage, status);

