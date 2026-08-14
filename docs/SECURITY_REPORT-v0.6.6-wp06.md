# V066-WP06 security report

## Threats addressed

- cross-project event disclosure;
- duplicate submit/cancel/checkpoint/resume side effects;
- stale revision writes;
- revision rollback after an idempotent replay;
- secret-bearing metadata or event payloads;
- non-finite JSON and malformed timestamps;
- accidental activation of third-party runtimes or paid calls.

## Controls

The HTTP handler derives project scope from the request header and checks it
before every runtime read. EventStore commands use idempotency fingerprints and
optimistic revisions. The Runtime Backend checks prior commands before stale
revision checks, while ControlKernel writes hold the established per-run lock
order. Runtime event payloads are sanitized and bounded. The public policy
reports local/adaptor-only behavior and external calls disabled.

## Residual risk

This is not authentication, a distributed lease service or a sandbox for
untrusted code. Provider-call spans remain process-local, arbitrary future
adapter fields need their own adversarial tests, and production network,
availability and multi-tenant deployment were not validated.
