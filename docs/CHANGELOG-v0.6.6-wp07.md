# CostWeave v0.6.6 — V066-WP07 Small-Budget Shadow

## Release status

WP07 is an E0 engineering increment. It adds a bounded Shadow execution
boundary, but does not provide production evidence or enable external calls by
default. `engineering_ready=true` may be reported only while every release
gate passes; `production_ready=false` remains authoritative.

## Added

- `costweave.small_budget_shadow` with strict experiment, probe, measurement,
  session and receipt schemas.
- Project-scoped experiment configuration and inspection APIs.
- Atomic pre-call reservations through the existing `ExperimentalCallStore`.
- Explicit Provider, deployment, data scope, expiry, per-call ceiling, total
  budget, Provider budget, call count, output-token and consent fields.
- Metrics-only persistence, with an optional output hash when the caller
  explicitly selects that data scope.
- A release self-test covering disabled-by-default admission, hard limits,
  redaction and advisory-only invariants.

## Safety behavior

- The normal server has no Shadow executor and both experimental-call switches
  are off, so configuring an experiment cannot cause a network or paid call.
- A remote session additionally requires a secret environment reference, a
  currently available secret value and explicit network and paid-call consent.
- Budget is reserved before an executor can run. One probe permits one attempt;
  WP07 never retries an ambiguous failure that may already have been billed.
- An executor failure without trustworthy billing telemetry consumes the
  reserved per-probe ceiling conservatively instead of releasing it.
- Raw WorkOrders, prompts, outputs and secret values are not written to the
  Shadow ledger. Executor errors are converted to bounded error codes.
- The baseline execution continues. A Shadow receipt cannot change the route,
  promote a policy or support a commercial claim.
- Active consent/configuration is process-local and must be re-armed after a
  restart; the durable budget ledger is retained.

## Compatibility

No database migration or new dependency is required. WP07 reuses migration
016's experimental call ledger and leaves the WP06 Runtime Backend, existing
run APIs and stable v0.5.8.4 contracts unchanged.

## Verification

- WP07 focused contract/runtime/API tests: **9/9 passed**;
- WP06/WP05/server/release compatibility subset: **59/59 passed**;
- complete discovery suite: **1011/1011 passed**;
- release self-test: **18/18 gates passed** with zero external calls.

The fixture executor is deterministic E0 evidence. It is not a real Provider,
real traffic, billing reconciliation, model-quality comparison or cost-saving
measurement.
