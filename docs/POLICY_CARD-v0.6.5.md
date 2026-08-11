# v0.6.5 policy card

Policy revision: trace-credit-assignment-safe-learning-v1.

The policy has six credit components: decomposition, routing, handoff,
execution, validation and recovery. Evidence levels E0 and E1 are never
learning-eligible. Learning requires evidence rank E2 or higher, an
independent validator, real production or human-benchmark origin, at least
eight observations, effective sample size of at least eight, calibration,
common support and a rollback policy.

Promotion checks are evidence, support, sample size, non-inferiority,
quality, budget, rollback readiness and advisory-only authority. Any failed
check rejects the candidate. Offline assessment can freeze an auditable
candidate; shadow assessment can mark it shadow-eligible; neither state
changes the actual route.
