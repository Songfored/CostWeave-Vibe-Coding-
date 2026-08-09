# v0.6.1 benchmark card

## Scope

This release adds contract and boundary tests for the Rational MetaController. It is an engineering verification report, not a claim that routing quality or cost has improved in production.

## Measured checks

- Deterministic replay with fixed beliefs and timestamp.
- Conservative `FAST` eligibility and mandatory `EXPERT` escalation for risk, OOD, stale evidence, capacity, and infeasible candidates.
- Adaptive committee selection with marginal-value, correlation, budget, and stop-reason checks.
- Atomic budget reserve/release/commit accounting across money, tokens, wall time, and call dimensions.
- Immutable, hash-verified persistence; project isolation; idempotency-key conflict detection.
- Loopback API confirmation, content-boundary, replay, lookup, and offline-call counters.

## Interpretation

The suite proves implementation contracts only. It has no real workload sample, no independently scored outputs, no E1+ evidence, and no provider latency/price observations. Therefore Decision Regret, CPS, success, quality, P95, and cost-reduction claims are `missing`, not zero and not positive.
