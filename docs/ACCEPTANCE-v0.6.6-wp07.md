# CostWeave v0.6.6 V066-WP07 acceptance

## Decision

**Accepted for E0 engineering scope.** The bounded Shadow control plane is
implemented, compatible with the existing suite and remains disabled by
default. `production_ready=false` remains authoritative.

## Evidence

| Gate | Result |
|---|---:|
| WP07 focused contract/runtime/API tests | 9/9 passed |
| WP06/WP05/server/release compatibility subset | 59/59 passed |
| Complete test discovery | 1011/1011 passed |
| Small-budget Shadow release gate | passed |
| Total release-readiness gates | 18/18 passed |
| Network/Provider calls made by acceptance fixtures | 0 |

## Verified behavior

- default admission blocks before a reservation or executor call;
- health/status remain available and report `runtime_available=false` when the
  optional persistent runtime is disabled;
- configuration is bounded, strict, idempotent, project-scoped and publicly
  redacted;
- a remote session cannot arm without current secret availability and both
  levels of paid/network authority;
- a permitted fixture call reserves budget first and executes once;
- replay returns stored metrics without executing again;
- failures stay inside Shadow and do not affect baseline execution;
- an ambiguous executor failure consumes the reserved ceiling conservatively;
- call-count exhaustion blocks the next probe;
- secret-bearing WorkOrders and output-token overruns fail closed;
- raw prompt/output/secret values are absent from the durable call record; and
- every receipt denies route changes, policy promotion and commercial claims.

## Remaining boundary

The only executing adapter in the tests is a deterministic in-process fixture.
Real Provider responses, invoices, caching, retries, P50/P95 latency, outcome
quality and production concurrency remain unmeasured. WP08 must reconcile
those facts before any real sample can support a routing or commercial claim.
