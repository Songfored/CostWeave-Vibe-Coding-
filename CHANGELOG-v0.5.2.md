# CostWeave v0.5.2

## Added

- Frozen eleven-dimensional task difficulty vector with confidence interval.
- Replaceable, zero-provider-call local classifier interface.
- Rule-first Planning Governor across all six planning modes.
- ExpectedLoss decomposition for money, time, failure, error risk and coordination.
- Value-of-Deliberation gate and planning stop conditions.
- Pre-execution local capacity, concurrency and deadline assessment.
- Mode-specific reasoning floors and normalized-price-aware role assignment.
- Low-confidence and high-risk escalation before execution.
- Explainable candidate comparison in API receipts and the web console.
- Runtime migration 010 and v4.9 / v5.1 Projection compatibility.
- Dedicated Planning Governor contract, routing, capacity and UI regression tests.

## Changed

- Product version is now 0.5.2.
- Runtime and Projection Schema are now `v5.2`.
- New runs use the Planning Governor rather than the v0.5.1 selector simulation.
- Planning decisions now persist difficulty, candidate, VoD and escalation evidence.
- Expert routing requires a concrete high-risk, low-confidence, capability-gap or difficulty trigger.

## Safety and scope

- No real model API is called and paid calls remain disabled by default.
- MultiPlanner candidates are evaluated, but true parallel proposal execution and adjudication remain v0.5.3 work.
- The heuristic classifier is deterministic and replaceable; it is not represented as a trained production policy.
