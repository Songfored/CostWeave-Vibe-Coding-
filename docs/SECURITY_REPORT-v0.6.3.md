# v0.6.3 security boundary report

- Joint-search requests require an explicit confirmation token and an
  idempotency key.
- Project scope is normalized and enforced for candidates and decisions.
- Candidate and decision records are append-only, hashed and protected by
  SQLite immutability triggers.
- Tool and authority scopes are checked before a candidate can be selected;
  shared handoff contradictions and information-loss limits fail closed.
- High-risk nodes require a validator distinct from the executor.
- Unknown or over-budget cost/latency estimates cannot be silently routed.
- The v0.6.3 path has no Provider, network, training or production route
  authority.

This is an engineering boundary report. It is not a claim of a completed
production security audit.
