# v0.6.6 V066-WP05 `fixed-final` security report

## Decision

The `fixed-final` artifacts passed E0 engineering security re-verification. This is
not a production security certification.

## Reviewed boundaries

- trace/span identifiers and project-scoped trace API access;
- prompt/secret pattern redaction and long-string removal;
- strict JSON, recursive sanitization and deep immutability;
- bounded recorder storage and weak-reference registry lifetime;
- provider adapter admission and context propagation;
- durable Trace ID propagation and restart reconstruction;
- local-only export and release-readiness gates;
- clean wheel/ZIP reproduction and checksum verification.

## Verified controls

`TracePolicyV1` rejects raw prompt or secret capture flags. Adversarial fixtures
cover authorization, Basic/Digest/bearer/API-key values, `auth`/`pwd` aliases,
nested secrets and exception text. Recognized sensitive values are replaced by
metadata-only markers (`redacted`, `kind`, `value_type`, optional `length`)
without a digest or preview. Non-finite floats are rejected and strict JSON is
used for local exports and HTTP responses. Frozen span attributes/events resist
post-construction mutation.

Built-in gateway hooks pass selected metadata and create provider spans only
after existing admission gates pass. The recorder lookup uses a thread-safe
`WeakValueDictionary`, so it does not retain otherwise unreachable recorders.
External export is not implemented. The `fixed-final` wheel passed all 16
release-readiness gates with zero external calls.

The final ordinary full run also exposed and closed a Windows TCP-reset path:
HTTP 415 could be returned before the request body was reliably consumed. The
server now applies a bounded 100 ms drain; a 20-consecutive-request regression
and both final full suites passed.

## Residual risk

- Pattern recognition cannot prove that every future credential encoding or
  third-party attribute is safe; integrations must keep an allowlist.
- Marker length and classification remain observable metadata.
- Weak lookup does not own lifetime; callers needing provider spans must retain
  a strong recorder reference.
- Provider-call spans are process-local; only event-derived lifecycle spans can
  be reconstructed after restart.
- No external collector authorization, retention, deletion, sampling or egress
  control has been implemented or reviewed.
- Production telemetry cost, availability and cross-process propagation remain
  unverified.

## Evidence

The `fixed-final` release passed 25/25 focused/adversarial tests, 94/94
compatibility tests, 147/147 affected tests, 995/995 full tests and 995/995
isolated tests.
`engineering_ready=true`; `production_ready=false`; external calls were zero.
