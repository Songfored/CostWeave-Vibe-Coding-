CREATE TABLE IF NOT EXISTS resilience_campaigns (
    campaign_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    campaign_kind TEXT NOT NULL CHECK(campaign_kind IN ('stress','fault','migration','recovery')),
    evidence_level TEXT NOT NULL CHECK(evidence_level IN ('E0-simulated','E1-replayed')),
    spec_json TEXT NOT NULL CHECK(json_valid(spec_json)),
    spec_hash TEXT NOT NULL UNIQUE CHECK(length(spec_hash)=64),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_resilience_campaigns_project_created
ON resilience_campaigns(project_id, created_at DESC);

CREATE TABLE IF NOT EXISTS resilience_observations (
    observation_id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL,
    observation_kind TEXT NOT NULL CHECK(observation_kind IN (
        'admission','rate_limit','provider_failure','failover','out_of_memory',
        'deadline_warning','cancellation','crash','restore','resource_leak','summary'
    )),
    observation_json TEXT NOT NULL CHECK(json_valid(observation_json)),
    observation_hash TEXT NOT NULL UNIQUE CHECK(length(observation_hash)=64),
    observed_at TEXT NOT NULL,
    FOREIGN KEY(campaign_id) REFERENCES resilience_campaigns(campaign_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_resilience_observations_campaign_time
ON resilience_observations(campaign_id, observed_at, observation_id);

CREATE TABLE IF NOT EXISTS recovery_checkpoints_v0583 (
    checkpoint_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    runtime_revision INTEGER NOT NULL CHECK(runtime_revision>=0),
    plan_json TEXT NOT NULL CHECK(json_valid(plan_json)),
    plan_hash TEXT NOT NULL UNIQUE CHECK(length(plan_hash)=64),
    created_at TEXT NOT NULL,
    UNIQUE(run_id, runtime_revision)
);

CREATE INDEX IF NOT EXISTS idx_recovery_checkpoints_v0583_project_run
ON recovery_checkpoints_v0583(project_id, run_id, runtime_revision DESC);

CREATE TABLE IF NOT EXISTS recovery_receipts_v0583 (
    receipt_id TEXT PRIMARY KEY,
    checkpoint_id TEXT NOT NULL,
    outcome TEXT NOT NULL CHECK(outcome IN ('planned','resumed','refused_terminal','failed')),
    receipt_json TEXT NOT NULL CHECK(json_valid(receipt_json)),
    receipt_hash TEXT NOT NULL UNIQUE CHECK(length(receipt_hash)=64),
    created_at TEXT NOT NULL,
    FOREIGN KEY(checkpoint_id) REFERENCES recovery_checkpoints_v0583(checkpoint_id) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_recovery_receipts_v0583_checkpoint
ON recovery_receipts_v0583(checkpoint_id, created_at, receipt_id);

CREATE TRIGGER IF NOT EXISTS resilience_campaigns_no_update
BEFORE UPDATE ON resilience_campaigns
BEGIN SELECT RAISE(ABORT, 'resilience campaigns are immutable'); END;

CREATE TRIGGER IF NOT EXISTS resilience_campaigns_no_delete
BEFORE DELETE ON resilience_campaigns
BEGIN SELECT RAISE(ABORT, 'resilience campaigns are immutable'); END;

CREATE TRIGGER IF NOT EXISTS resilience_observations_no_update
BEFORE UPDATE ON resilience_observations
BEGIN SELECT RAISE(ABORT, 'resilience observations are immutable'); END;

CREATE TRIGGER IF NOT EXISTS resilience_observations_no_delete
BEFORE DELETE ON resilience_observations
BEGIN SELECT RAISE(ABORT, 'resilience observations are immutable'); END;

CREATE TRIGGER IF NOT EXISTS recovery_checkpoints_v0583_no_update
BEFORE UPDATE ON recovery_checkpoints_v0583
BEGIN SELECT RAISE(ABORT, 'recovery checkpoints are immutable'); END;

CREATE TRIGGER IF NOT EXISTS recovery_checkpoints_v0583_no_delete
BEFORE DELETE ON recovery_checkpoints_v0583
BEGIN SELECT RAISE(ABORT, 'recovery checkpoints are immutable'); END;

CREATE TRIGGER IF NOT EXISTS recovery_receipts_v0583_no_update
BEFORE UPDATE ON recovery_receipts_v0583
BEGIN SELECT RAISE(ABORT, 'recovery receipts are immutable'); END;

CREATE TRIGGER IF NOT EXISTS recovery_receipts_v0583_no_delete
BEFORE DELETE ON recovery_receipts_v0583
BEGIN SELECT RAISE(ABORT, 'recovery receipts are immutable'); END;
