-- v0.5.2 planning decisions remain in the authoritative event stream and
-- projection. This compound index supports per-run governor audit queries.
CREATE INDEX IF NOT EXISTS idx_events_run_kind_revision
    ON events(run_id, kind, stream_revision);

