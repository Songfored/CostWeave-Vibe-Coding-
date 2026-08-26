# CostWeave v0.6.6 — V066-WP06 Runtime Backend

## Release status

WP06 is an E0 engineering increment. The local persistent Runtime Backend
contract is implemented and verified; `engineering_ready=true` remains valid
when the existing release gates pass, while `production_ready=false` and
commercial claims remain disabled.

## Added

- `costweave.runtime_backend` with strict `RuntimeSubmitRequestV1`,
  `RuntimeCapabilitiesV1`, `RuntimeOperationV1`, `RuntimeEventV1` and
  `RuntimeEventPageV1` schemas.
- `RuntimeBackend` protocol and `LocalRuntimeBackend` implementation around
  the existing persistent `RunManager`/`ControlKernel`.
- Idempotent `submit`, `cancel`, `checkpoint` and `resume` operations with
  optimistic runtime revision checks.
- Project-scoped, paginated event access with sanitized payloads and detached
  exports.
- HTTP endpoints: `/api/v1/runtime/status`, `/api/v1/runtime/submit` and
  `/api/v1/runtime/<run_id>/{cancel,checkpoint,resume,events}`.
- A release self-test for the local-first, adapter-only runtime policy.

## Correctness and safety

- Durable checkpoint/resume commands replay before revision validation, so a
  retry cannot create a second event or move the in-memory revision backwards.
- Internal engine checkpoints hold the per-run lock while constructing their
  revision-derived idempotency key, preventing races with public checkpoints.
- Foreign project IDs receive the same not-found behavior as existing run APIs.
- Metadata and event payloads reject or sanitize secret-shaped values; strict
  finite-JSON and timestamp validation applies at the boundary.
- Third-party runtimes are represented only by the protocol. No runtime SDK,
  network request, provider call or paid call is imported or enabled.

## Compatibility

No database migration, runtime dependency or existing run API schema change is
required. The existing `RunManager.create`, `cancel`, events and v0.5.8.4
stable contracts remain available.

## Verification

- WP06 focused contract/API tests: **7/7 passed**;
- existing WP05 observability/release subset: **39/39 passed**;
- complete discovery suite: **1002/1002 passed**;
- release self-test: **17/17 gates passed** with external calls disabled.

Evidence is local and deterministic. It does not prove distributed runtime
availability, third-party runtime compatibility, production throughput, real
provider quality or cost reduction.
