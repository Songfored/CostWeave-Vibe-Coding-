-- v0.5.3 stores identity-blind scoring, conflicts, and bounded adjudication
-- in the authoritative event stream and run projection. This index supports
-- cross-run deliberation audits without introducing a second source of truth.
CREATE INDEX IF NOT EXISTS idx_events_kind_run_revision
    ON events(kind, run_id, stream_revision);
