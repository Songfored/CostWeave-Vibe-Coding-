# v0.6.6 V066-WP05 `fixed-final` data card

## Verification status

The `fixed-final` artifacts passed E0 engineering re-verification: 25/25
focused/adversarial, 94/94 compatibility, 147/147 affected, 995/995 full and
995/995 isolated tests, plus 16/16 release-readiness gates with zero external
calls. This card is not a production privacy certification.

## Intended inputs

Built-in WP05 integrations pass run/task IDs, gateway request/model/backend
identifiers, stage names, bounded statuses and selected operational metadata.
They are designed not to pass raw messages, provider payloads, headers or
credentials.

`TraceRecorder` is also a public programmatic surface that accepts attribute
mappings. Third-party integrations must use an allowlist and must not send raw
user/provider content, headers, credentials or unrestricted exception text.

## Transformations

Trace IDs use a domain-separated SHA-256 digest of the run ID. This digest is
used only to derive the stable Trace ID; it is distinct from value redaction.

Recognized prompt/secret keys and secret-shaped values are replaced by a
metadata-only marker containing `redacted`, `kind`, `value_type` and optional
`length`. The marker retains no source-value hash, digest or preview. Long
unclassified strings are similarly replaced without a digest or preview.
Non-finite floats are rejected, nested values are sanitized recursively and
published span attributes/events are deeply immutable.

## Outputs and persistence

The public run snapshot exposes an observability summary. The trace endpoint
exposes bounded local spans, while `TraceRecorder.to_otel()` provides an
offline OTLP-shaped representation. Durable runtime events and projections
carry the stable Trace ID alongside their pre-existing data.

Lifecycle spans derivable from durable events can be reconstructed after a
restart. Provider-call spans are process-local and are neither persisted nor
reconstructed. WP05 generates no training dataset or real provider billing
ledger.

## Registry and retention

The process-global lookup is a thread-safe `WeakValueDictionary`. It does not
own recorder lifetime and is not a fixed-capacity cache. Strong ownership stays
with the `RunRecord` or integrating caller. Span count, event count and
attribute-size limits bound each live recorder; no external retention system is
implemented.

## Evidence and limitations

The `fixed-final` suites verified Basic/Digest/bearer/API-key values, `auth`/`pwd`
aliases, nested secrets, exception text, strict JSON and deep immutability.
Those fixtures cannot prove that every future third-party value is safe. No
collector retention/deletion guarantee, cross-process provider tracing,
production traffic, real billing reconciliation or universal secret detection
is claimed.
