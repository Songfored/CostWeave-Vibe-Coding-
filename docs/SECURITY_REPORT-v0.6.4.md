# v0.6.4 security boundary report

## Verified controls

- no Provider, network or training call exists in the v0.6.4 path;
- snapshots, beliefs and decisions are project-scoped and hash-validated;
- decision and idempotency tables are append-only through SQLite triggers;
- project mismatch and idempotency reuse fail closed;
- budget, deadline, capacity, retry and replan limits are hard checks;
- the service cannot modify the stable production route;
- malformed DAGs and missing confirmation are rejected.

## Evidence boundary

This is an offline engineering security report. It does not certify a
production deployment, provider credential handling, distributed tenancy or
real-world availability.
