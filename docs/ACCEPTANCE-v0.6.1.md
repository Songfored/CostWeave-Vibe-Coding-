# CostWeave v0.6.1 acceptance record

## Release identity

- Version: `0.6.1`
- Runtime schema: `v6.1.0`
- Migrations: `31`
- Active policy: `rational-meta-controller-v1`
- Authority: advisory-only

## Acceptance criteria

- [x] Fast path is conservative and deterministic.
- [x] High risk, OOD, stale evidence, capacity, side effects, infeasible strategies, and budget conditions have explicit reason codes.
- [x] Planner committee is adaptive, bounded, correlation-aware, and budget-aware.
- [x] EVC is explicit and uses a common utility unit.
- [x] Probe has no side-effect authority; expert escalation does not expand permissions.
- [x] Budget ledger is multi-dimensional, atomic, immutable, and hash-verified.
- [x] API and CLI reject unknown/unauthorized input and persist idempotently.
- [x] Existing v0.6.0 tournament contracts remain available.
- [x] Default provider and network calls are zero.

## Evidence boundary

The acceptance suite is deterministic E0 engineering evidence. It validates code paths and contracts but does not prove real model quality, production throughput, commercial savings, or superiority over a competitor. Those remain explicit v0.6.2+ exit gates.
