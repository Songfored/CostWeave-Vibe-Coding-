# CostWeave v0.6.6 acceptance

## Scope

This acceptance covers V066-WP01, the provider-neutral Gateway Backend
contract. It does not accept a real provider integration.

## Required gates

- Package version and release policy report v0.6.6.
- Runtime schema v6.5.0 and migration 035 remain intact.
- Request, capability, usage, billing, error, event, response and cancellation
  objects round-trip through strict JSON dictionaries.
- Unknown fields, secret-like metadata, invalid identifiers, duplicate tools,
  invalid usage/billing and status inconsistencies fail closed.
- Stream events are correlated to the request and strictly ordered.
- Streaming and non-streaming response modes are checked before planner use.
- Gateway policy reports network, provider and paid calls disabled by default.
- Focused and full regression suites pass.

## Evidence boundary

All evidence in this release is offline E0 engineering evidence. The release
does not measure provider quality, savings, latency, high-concurrency capacity
or competitive superiority.

## Results

- Focused contract/release/safe-learning/event/UI tests: **60 passed**.
- Full regression suite: **924 passed in 321.691 seconds**.
- Release CLI: engineering gates passed, runtime v6.5.0, migration 035, WAL,
  zero external-call attempts and `production_ready=false`.
- Real provider calls, paid calls and network calls: **0**.

The wheel and archive SHA-256 values are recorded in the generated
`v0.6.6-SHA256SUMS.txt` manifest.
