# CostWeave v0.6.4 data card

## Dataset scope

The v0.6.4 implementation stores execution-time beliefs, node outcomes,
artifact acceptance/reuse signals, validator-family observations and recovery
decisions. The shipped demo is a deterministic E0 fault-injection fixture; it
contains no user prompts, provider responses, production traces or network
data.

## Permitted use

- Contract and serialization regression tests;
- Offline recovery-policy demonstrations;
- Local ablations of continuation, retry, switch, request-info, subgraph
  replan, full replan and partial-return actions;
- Engineering review of budget, deadline, pollution and validator-diversity
  gates.

## Prohibited inference

The fixture must not be used to claim real model quality, production failure
rates, latency improvement, cost reduction, provider superiority or commercial
readiness. E0 observations cannot promote a policy into the production route.

## Governance

Records are project-scoped, idempotent and append-only. Stable identities omit
observation time but include the business fields and policy revision. Sunk cost
is recorded for audit and excluded from recovery optimization. External and
paid adapters remain disabled by default; any future E1+ dataset needs source,
consent, retention, redaction, split, leakage and rollback evidence before it
can affect routing.
