# v0.6.5 security report

## Controls

- all safe-learning writes require an explicit confirmation token;
- write endpoints require an Idempotency-Key;
- all persisted records are project-scoped, hash-verified and immutable;
- exact-field validation rejects unexpected input and non-finite numbers;
- candidates cannot mutate routes, auto-promote or claim commercial results;
- the default runtime makes zero Provider and network calls.

## Residual risks

Callers can mislabel real data, supply biased propensities or provide a weak
independent Validator. The service therefore fails closed on evidence,
support, calibration, sample and rollback gates. Operational deployments
still need access control, backup protection and human approval.
