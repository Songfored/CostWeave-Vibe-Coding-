# CostWeave v0.6.4 — Execution-Time Belief Update & Economic Recovery

v0.6.4 adds a provider-free dynamic decision loop after execution has started.
It turns node results, milestone checks, artifact acceptance, pollution scope,
remaining budget, deadline and resource changes into a conservative
`ProgressBeliefV1`, then compares seven bounded recovery actions.

## Added

- `ExecutionSnapshotV1`, `ProgressBeliefV1`, `RecoveryOptionV1` and
  `ExecutionRecoveryDecisionV1` contracts;
- critical-path and pollution propagation for hard/validation dependencies;
- sunk-cost exclusion, validated Artifact reuse credit and switching cost;
- failure-correlation-aware orthogonal Validator checks;
- `CONTINUE`, `RETRY_NODE`, `SWITCH_MODEL`, `REQUEST_MISSING_INFO`,
  `REPLAN_SUBGRAPH`, `FULL_REPLAN` and `STOP_AND_RETURN_PARTIAL` comparison;
- immutable project-scoped SQLite records and idempotency;
- offline API, CLI and UI demonstration;
- migration 034 and runtime schema `v6.4.0`.

## Boundary

The controller is advisory-only. It does not execute a Provider, access a
network, modify the stable route or train a policy. The bundled fault-injection
fixture is E0 simulated evidence and cannot support real savings, quality,
latency or competitive claims.
