# v0.6.6 V066-WP05 `fixed-final` acceptance

## Decision

**Engineering re-verification passed on the `fixed-final` artifacts.**

- `engineering_ready=true`;
- `production_ready=false`;
- release-readiness gates: 16/16 passed;
- external calls: 0;
- evidence level: E0 local engineering evidence.

This is not a production-security, collector-interoperability or commercial
performance approval.

## Verified artifact names

- `v066-wp05-fixed-final-dist/costweave-0.6.6-py3-none-any.whl`;
- `costweave-v0.6.6-wp05-fixed-final-release.zip`;
- `costweave-v0.6.6-wp05-fixed-final-source-release.zip`;
- `SHA256SUMS-v0.6.6-wp05-fixed-final.txt`;
- `costweave-v0.6.6-wp05-fixed-final-acceptance-20260814.md`.

The original and pre-final `fixed` artifacts are superseded and are not covered
by this decision. The `fixed-final` checksum manifest and acceptance report
bind the final files to this rerun.

## Functional and safety gates

- each run receives one stable 32-hex Trace ID;
- projections, public snapshots and durable runtime event payloads carry the
  same ID;
- Goal, Plan, Task, Provider Call, Validation and Recovery stages are
  representable under that ID;
- OpenAI-compatible and LiteLLM provider spans begin only after their existing
  admission gates pass;
- built-in integrations pass selected metadata rather than raw provider
  messages or payloads;
- prompt keys, Basic/Digest/bearer/API-key values, `auth`/`pwd` aliases, nested
  secrets and exception-text fixtures are removed;
- redaction markers contain classification metadata only: `redacted`, `kind`,
  `value_type` and optional `length`; no value hash, digest or preview remains;
- requests to enable raw prompt or secret capture are rejected;
- non-finite floats are rejected and human/OTLP-shaped exports remain strict
  JSON;
- span storage, event counts and attribute sizes are bounded and overflow is
  counted;
- frozen span attributes/events are deeply immutable;
- W3C `traceparent` and offline OTLP-shaped fields satisfy their local contract;
- the trace API enforces run project scope;
- external exporter support is absent and disabled by default;
- clean wheel installation, runtime/source archive extraction and SHA-256
  verification complete without external or paid calls.

## Verified test evidence

| Verification layer | Result |
|---|---:|
| WP05 focused and adversarial | 25/25 confirmed |
| Compatibility subset | 94/94 |
| Affected combination | 147/147 |
| Full unittest discovery | 995/995 |
| Process-isolated full suite | 995/995 |
| `fixed-final` wheel release readiness | 16/16 gates |

All listed layers completed with zero failures and errors. The `fixed-final`
wheel release run recorded zero external calls.

## HTTP 415 regression closed during acceptance

The ordinary full suite twice reproduced a Windows `ConnectionAbortedError` in
an older tournament Content-Type rejection case. The server was returning 415
without reliably consuming the small request body, allowing a TCP reset. The
fix adds a bounded 100 ms body drain before the response. The affected suite
includes a 20-consecutive-request regression; it, the final ordinary full suite
and the isolated full suite all passed.

## Lifetime and restart boundary

The global recorder lookup uses a thread-safe `WeakValueDictionary`; it is not
a fixed-capacity owner registry. A `RunRecord` or integrating caller must keep a
strong recorder reference while provider spans are required.

The stable Trace ID and event-derived lifecycle spans can be reconstructed
after restart. Provider-call spans remain process-local and are not persisted
or reconstructed in WP05. This limitation is accepted within the explicit
`E0-local-in-memory` scope, not hidden as durable tracing.

## Evidence boundary

The verified fixtures demonstrate the listed contract and adversarial cases;
they do not prove that every future third-party attribute is credential-free.
Integrations must continue to use allowlisted metadata. WP05 does not establish
external collector receipt, distributed propagation, production sampling cost,
production availability, provider answer quality, real billing accuracy, real
cost reduction or commercial superiority.
