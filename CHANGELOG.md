# CostWeave v0.6.6 — V066-WP05 `fixed-final` OpenTelemetry-compatible tracing

## Release status

Engineering re-verification passed on the `fixed-final` artifacts.
`engineering_ready=true`, `production_ready=false`, all 16 release-readiness
gates passed and external calls remained zero.

## Added

- `costweave.observability` with `TracePolicyV1`, `TraceContextV1`,
  `TraceSpanV1` and a bounded thread-safe `TraceRecorder`.
- Stable per-run 32-hex Trace IDs and W3C-style `traceparent` values.
- Goal, Plan, Task, Provider Call, Validation and Recovery stages.
- Human-readable local trace export, offline OTLP-shaped serialization and
  `GET /api/v1/runs/<id>/trace`.
- Trace ID propagation through `RunRecord` projections and durable EventStore
  payloads.
- Provider-call span hooks in the OpenAI-compatible and LiteLLM adapters.
- Release self-tests for redaction, strict JSON and deep immutability.

## Corrected before the `fixed-final` release

- Expanded sensitive-key/value handling for Basic/Digest/bearer/API-key
  values, `auth`/`pwd` aliases, nested secrets and exception text.
- Removed source-value SHA-256 digests and previews from redaction output.
  Markers now contain only `redacted`, `kind`, `value_type` and optional
  `length` metadata.
- Rejected `NaN` and `Infinity` and made trace/HTTP serialization strict JSON.
- Deep-froze published attributes and events against later caller mutation.
- Replaced the owning fixed-capacity recorder registry with a thread-safe
  `WeakValueDictionary` lookup.
- Clarified that provider-call spans are process-local; only event-derived
  lifecycle spans reconstruct after restart.
- Fixed the tournament HTTP 415 response path on Windows by performing a
  bounded 100 ms request-body drain before responding; the prior behavior could
  produce `ConnectionAbortedError`/RST. Added a 20-consecutive-request
  regression.
- Completed `fixed-final` wheel, runtime ZIP, source ZIP, checksum and acceptance
  artifacts.

## Verification

- focused/adversarial: 25/25 confirmed;
- compatibility: 94/94;
- affected combination: 147/147;
- ordinary full suite: 995/995;
- process-isolated full suite: 995/995;
- `fixed-final` wheel release readiness: 16/16, external calls 0.

## Compatibility and evidence boundary

No database migration, runtime dependency or projection-schema bump is
required. Evidence remains E0 local engineering evidence: it does not prove
collector interoperability, durable provider spans, distributed propagation,
production telemetry overhead, provider quality, real cost reduction or
commercial superiority.

The authoritative `fixed-final` artifact names are documented in
`INSTALL_REPRODUCE-v0.6.6-wp05.md`. Original and pre-final `fixed` artifacts
are not covered by this release decision.
