# v0.6.1 security boundary report

The Rational MetaController is intentionally an advisory, provider-free control-plane component.

- The service is loopback-only through the existing server boundary and defaults to zero network/provider calls.
- Requests are bounded JSON, strict UTF-8, exact-field validated, finite-number checked, and require `RUN_META_CONTROLLER` confirmation.
- Project IDs and idempotency keys are bounded and scoped. A key cannot be reused with different request content.
- Decisions, idempotency bindings, budgets, and ledger events are immutable and hash-verified. SQLite triggers reject update/delete tampering.
- Budget settlement is atomic; reserve must fit every dimension, release/commit must match the reservation amount, and spent amounts are never rolled back.
- `probe` carries no execution authority. `expert` transfers a decision; it does not grant tools, credentials, network access, or permission to mutate a project.
- The management-depth limit is two levels; recursive planner creation is outside the contract.

This is an application-layer boundary report, not an operating-system sandbox, public-authentication, or formal security proof. External adapters remain disabled unless a future release explicitly introduces and independently validates them.
