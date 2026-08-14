# CostWeave v0.6.6 V066-WP06 acceptance

## Decision

**Accepted for E0 engineering scope.** The local-first Runtime Backend is
implemented, project-scoped, idempotent and compatible with the existing
persistent run control plane. `production_ready=false` remains authoritative.

## Evidence

| Gate | Result |
|---|---:|
| WP06 focused contract/API tests | 7/7 passed |
| WP05 observability/release compatibility subset | 39/39 passed |
| Complete test discovery | 1002/1002 passed |
| Runtime policy release gate | passed |
| Total release-readiness gates | 17/17 passed |

The full suite includes the prior v0.5/v0.6 contracts, API/UI tests, gateway
tests, persistence/replay tests, WP05 adversarial tracing tests and the new
runtime backend tests. No provider or network call was made by the WP06
fixtures.

## Verified behavior

- first submit returns HTTP 202 and an operation with a stable run/Trace ID;
- replaying the same key returns the same run with `replayed=true`;
- checkpoint and resume are durable and replay-safe;
- stale revisions are rejected while a prior replay is accepted;
- event pages are ordered, bounded, sanitized and project-scoped;
- foreign project event access returns 404;
- exported snapshots are detached from internal state;
- terminal runs cannot be resumed;
- third-party runtime policy remains adapter-only and external calls remain off.

## Remaining boundary

The implementation is a local single-process adapter. It does not establish
distributed leases, cross-process event streaming, third-party runtime
compatibility, production scheduling capacity, real provider quality, real
cost savings or superiority over another product.
