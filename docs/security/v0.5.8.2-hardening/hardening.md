# Security Hardening Review: CostWeave v0.5.8.2

## Evidence Basis

We inspected the Adapter, external-call, evidence-export, HTTP API, run-scope and migration owners listed in [context.md](context.md). The source supports one cross-cutting conclusion: secret access, network admission, redaction, project isolation and side-effect replay are individually guarded, but no single boundary owns all five invariants. This is source-derived engineering evidence; it does not establish exploitability or production exposure.

## Constraints

We assume a balanced change profile: preserve the default-off, loopback-only prototype and existing v0.5 APIs; add no paid calls; avoid a new service or process in this release; fail closed when project, secret, endpoint, prompt or side-effect identity is ambiguous. No measured performance or memory budget was supplied.

## Opportunity Portfolio

| Opportunity | Evidence | Options | Recommendation | Proposal |
| --- | --- | --- | --- | --- |
| Centralize privileged Adapter and data-scope enforcement | Ambient secrets/network (E001), replay state (E002), local redaction (E003), unscoped APIs (E004–E006) | 1. Local guards; 2. Central security boundary; 3. Isolated Adapter gateway | Implement Option 2 now; retain Option 3 for production connectors | [Proposal](proposals/central-security-boundary.md) |

## Recommendation Summary

I recommend Option 2 under the current constraints. It removes ambient security decisions from individual Adapter and API call sites without introducing the deployment and latency burden of a separate gateway. The attractive part of Option 1 is its small diff, but future connectors could still omit a guard. Option 3 becomes preferable once CostWeave accepts untrusted plugins or production credentials, because process isolation would then justify its operational cost.

The user has already selected the v0.5.8.2 roadmap scope, so the implementation handoff is recorded in [implementation/central-security-boundary.md](implementation/central-security-boundary.md).

## Next Decisions

- Decide whether v0.6 production connectors require a separate process boundary.
- Decide when logical project revocation must become verified physical erasure or cryptographic erasure.
- Supply real concurrency and Adapter latency budgets before claiming negligible overhead.
