-- v0.5.4 persists the deployment posterior, latency forecast and
-- pre-execution admission receipt alongside each placement.
ALTER TABLE placements
    ADD COLUMN decision_json TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_capability_posteriors_level_updated
    ON capability_posteriors(level, updated_at);

CREATE INDEX IF NOT EXISTS idx_placements_provider_status
    ON placements(provider, status, created_at);
