-- v0.5.1 keeps planning decisions inside the authoritative run projection and
-- event stream. This index makes policy/mode audit queries efficient without
-- creating a second mutable source of truth.
CREATE INDEX IF NOT EXISTS idx_events_kind_occurred
    ON events(kind, occurred_at);

