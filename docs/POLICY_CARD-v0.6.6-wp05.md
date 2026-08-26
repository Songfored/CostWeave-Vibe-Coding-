# v0.6.6 V066-WP05 `fixed-final` policy card

## Verification status

The `fixed-final` artifacts passed engineering re-verification. Production and
commercial claims remain prohibited.

- Local recording: enabled by default; each recorder defaults to at most 512
  spans, with bounded events and attribute bytes.
- Recorder lookup: thread-safe weak references; the registry does not own
  lifetime and has no fixed-capacity claim.
- External exporter: unsupported and disabled by default.
- Network and paid calls initiated by observability: zero.
- Raw prompt/secret capture flags: rejected by `TracePolicyV1`.
- Built-in attribute policy: selected operational metadata only.
- Pattern redaction: verified prompt/secret keys, Basic/Digest/bearer/API-key
  values, `auth`/`pwd` aliases, nested secrets and exception-text fixtures are
  replaced.
- Redaction marker: `redacted`, `kind`, `value_type` and optional `length`
  only; no source-value digest or preview.
- Long unclassified strings: metadata marker with no digest or preview.
- Finite values: `NaN` and `Infinity` are rejected; exports use strict JSON.
- Published span data: deeply immutable.
- Trace ID: stable per run and propagated through durable runtime events.
- Gateway provider span: begins only after existing admission gates pass.
- Provider-span retention: process-local; no restart persistence claim.
- Route intervention, provider retry authority and automatic learning: none.
- HTTP error responses: unsupported Content-Type handling performs a bounded
  100 ms request-body drain before 415; the Windows RST regression passed 20
  consecutive requests.
- `fixed-final` wheel release readiness: 16/16 gates, zero external calls.
- Production readiness: false.

Third-party integrations remain responsible for allowlisting attributes. The
verified fixture set is a strong engineering guard, not proof that all future
credential encodings are recognizable.
