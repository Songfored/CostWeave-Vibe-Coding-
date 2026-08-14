CREATE TABLE IF NOT EXISTS resource_pools (
    pool_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    local INTEGER NOT NULL,
    slots_total INTEGER NOT NULL,
    cpu_cores_total REAL NOT NULL,
    gpu_count_total INTEGER NOT NULL,
    vram_mb_total INTEGER NOT NULL,
    queue_limit INTEGER NOT NULL,
    token_capacity REAL NOT NULL,
    token_refill_per_second REAL NOT NULL,
    state TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS resource_snapshots (
    snapshot_id TEXT PRIMARY KEY,
    pool_id TEXT NOT NULL,
    slots_used INTEGER NOT NULL,
    cpu_cores_used REAL NOT NULL,
    gpu_count_used INTEGER NOT NULL,
    vram_mb_used INTEGER NOT NULL,
    queue_depth INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    FOREIGN KEY (pool_id) REFERENCES resource_pools(pool_id)
);

CREATE INDEX IF NOT EXISTS idx_resource_snapshots_pool_time
    ON resource_snapshots(pool_id, captured_at);

CREATE TABLE IF NOT EXISTS resource_reservations (
    reservation_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    pool_id TEXT NOT NULL,
    slots INTEGER NOT NULL,
    cpu_cores REAL NOT NULL,
    gpu_count INTEGER NOT NULL,
    vram_mb INTEGER NOT NULL,
    status TEXT NOT NULL,
    lease_expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    release_reason TEXT,
    UNIQUE (run_id, task_id, attempt),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, task_id)
        REFERENCES tasks(run_id, task_id) ON DELETE CASCADE,
    FOREIGN KEY (pool_id) REFERENCES resource_pools(pool_id)
);

CREATE INDEX IF NOT EXISTS idx_resource_reservations_active
    ON resource_reservations(pool_id, status, lease_expires_at);

CREATE TABLE IF NOT EXISTS cost_reservations (
    cost_reservation_id TEXT PRIMARY KEY,
    reservation_id TEXT NOT NULL UNIQUE,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    amount REAL NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    released_at TEXT,
    actual_cost REAL,
    FOREIGN KEY (reservation_id)
        REFERENCES resource_reservations(reservation_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cost_reservations_run_status
    ON cost_reservations(run_id, status);

CREATE TABLE IF NOT EXISTS approvals (
    approval_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    approval_kind TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT NOT NULL,
    revision INTEGER NOT NULL,
    requested_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_note TEXT,
    resolution_key TEXT UNIQUE,
    UNIQUE (run_id, task_id, approval_kind),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    FOREIGN KEY (run_id, task_id)
        REFERENCES tasks(run_id, task_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_approvals_status_time
    ON approvals(status, requested_at);

CREATE TABLE IF NOT EXISTS provider_circuits (
    provider TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    consecutive_failures INTEGER NOT NULL,
    opened_at REAL,
    probe_in_flight INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
