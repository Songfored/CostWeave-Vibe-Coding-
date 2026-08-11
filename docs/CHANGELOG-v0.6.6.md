# CostWeave v0.6.6 — Gateway Backend Contract (WP01)

## Added

- Provider-neutral request, message and tool contracts.
- Capability advertisement for protocol, deployment, modalities, streaming,
  tools, structured output, usage, billing and cancellation.
- Ordered streaming events with request correlation and provider request ID.
- Structured usage, billing, error and cancellation records with strict
  invariants and round-trip serialization.
- Default-off public gateway policy and contract-level response validation.
- Focused regression coverage for boundary, secret, status, ordering and
  round-trip failures.

## Changed

- Package and release line are now v0.6.6.
- Server health identifies v0.6.6 as the active development line.
- Existing v0.6.5 safe-learning persistence and runtime migration set remain
  unchanged.

## Not included

No provider SDK, endpoint, socket, paid call, shadow traffic, telemetry export,
online learning or production readiness claim is included in this work package.
