# Implementation Plan: Central security boundary

## Selected Design And Constraints

Implement Option 2 from the hardening proposal. Preserve single-process operation, loopback-only HTTP, default-off external calls and API compatibility. Do not add real Provider traffic.

## Source Revision And Drift Check

The selected design is anchored to source collection `fab1b89c7d4de5941c849e04e9b9554f56c5920178711fd18981cf643634c624`. No Git revision exists in this workspace, so drift is unknown. Refresh the seven evidence paths before final packaging and record any relevant changes in the release acceptance report.

## Affected Components

- `costweave/security_boundary.py`
- `costweave/security_store.py`
- `costweave/adapters/http.py`
- `costweave/domain.py`, `costweave/projections.py`, `costweave/engine.py`, `costweave/control_kernel.py`
- `costweave/evidence_console.py`, `costweave/evidence_console_store.py`
- `costweave/server.py` and web assets
- migration 027 and v0.5.8.2 tests

## Ordered Work Packages

1. Add versioned security contracts, project normalization, redaction, prompt assessment, Adapter manifests, endpoint policy and secret leases.
2. Add append-only project tombstones and side-effect contract/events.
3. Integrate Adapter admission, Provider idempotency and bounded no-redirect transport.
4. Persist project/residency in runs and enforce scope on public run-derived reads and writes.
5. Reuse redaction for public JSON, logs and evidence exports.
6. Add policy API/UI, tests, benchmarks, migration and release artifacts.

## Compatibility And Migration

Old runs hydrate as project `local-default` with residency `local`. Existing evidence receipts also remain visible only to `local-default`. External calls remain disabled unless the existing explicit switches are set.

## Tactical Protections During Migration

Retain loopback binding, Origin/Host/Sec-Fetch checks, CSP, tool-free HTTP Adapters, hard budgets, external-call reservations and metadata-only Artifact APIs.

## Tests And Security Validation

Cover secret rotation and zero-leak output, unknown Adapter rejection, endpoint/redirect/body bounds, prompt injection, tool denial, project isolation, tombstone fail-close, side-effect replay/conflict/ambiguous state, migration immutability, policy API and old snapshot hydration.

## Performance And Resource Benchmarks

Measure pure admission and append-only reservation P50/P95. Treat results as local engineering measurements only; do not infer production overhead.

## Rollout And Rollback

Ship the security boundary enabled for local policy checks while external calls stay off. On regression, disable experimental Adapters and revert code to v0.5.8.1; migration 027 records remain inert and must not be deleted.

## Acceptance Criteria

- Secret leak corpus: zero public or persistent occurrences.
- Unauthorized network/tool attempts: zero transport calls.
- Cross-project run/Outcome/Artifact visibility: zero.
- Every external side effect: Provider idempotency forwarded or explicit compensation; conflicting reuse rejected.
- Full historical suite, browser QA, isolated Wheel install and archive integrity pass.

## Open Decisions

Physical/cryptographic project erasure and process-isolated plugins remain v0.6 decisions.
