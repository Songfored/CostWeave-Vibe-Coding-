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

## Execution record

- Full isolated regression: **857 tests passed, 0 failed**, elapsed approximately 316 seconds.
- Focused v0.6.1 suite: **15 tests passed, 0 failed**.
- Release readiness: **success**; runtime schema `v6.1.0`, 31 migrations, WAL, event integrity, replay parity, backup verification, and default-off external/paid-call gates all passed.
- Wheel smoke test: `costweave-0.6.1-py3-none-any.whl` imports as `costweave 0.6.1` and runs the offline MetaController demo with provider/network calls equal to zero.

## Published local artifacts

- `outputs/costweave-v0.6.1.zip`
- `outputs/costweave-v0.6.1-source.zip`
- `outputs/v061-dist/costweave-0.6.1-py3-none-any.whl`
- `outputs/costweave-v0.6.1-SHA256SUMS.txt`
