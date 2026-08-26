# CostWeave v0.6.0 security boundary report

The v0.6.0 tournament path is local-only and fail-closed by default. It accepts bounded UTF-8 JSONL/CSV inputs, rejects unknown or malformed fields, hashes raw prompt/prediction text before creating a routing record, rejects project mixing, and never executes imported content.

The API requires loopback access, JSON content type, explicit confirmation, and an idempotency key. Tournament reports are persisted in immutable project-scoped rows. Provider calls and network calls are hard-coded to zero for the bundled offline service.

This is an engineering boundary report, not a production security certification. The v0.5.8.4 security report remains the baseline for the broader runtime.
