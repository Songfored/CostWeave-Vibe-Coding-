# CostWeave v0.6.1 — Rational MetaController

`v0.5.8.4` remains the frozen stable compatibility baseline and `v0.6.0` remains the historical Decision Intelligence Lab. v0.6.1 is an additive, offline control-plane release. It does not activate providers, rewrite historical decision-intelligence tables, or make a commercial performance claim.

## What changed

- Added `MetaControllerEngine` with four auditable actions: `fast`, `deliberate`, `probe`, and `expert`.
- Added conservative utility intervals, EVC accounting, hard-constraint/risk/OOD/stale-evidence/capacity gates, and a two-level management-depth policy.
- Added adaptive planner-committee selection by marginal information value, planning money, latency, and correlated-failure discount. The committee is bounded and stops at consensus, budget exhaustion, or non-positive marginal EVC; it is never a fixed “three model” fan-out.
- Added a multi-dimensional planning budget (`USD`, input/output tokens, wall time, planner/judge/probe calls) with atomic reservation, release, and commit events.
- Added immutable, hash-verified MetaDecision and idempotency persistence in migration 031. Project scope is normalized and enforced.
- Added `RationalMetaControllerService`, CLI, loopback API, and UI panel. The service requires `confirm=RUN_META_CONTROLLER`, a bounded `Idempotency-Key`, and rejects unknown fields.
- Added 15 focused contract/API/persistence tests while retaining the historical v0.6.0 tournament contracts unchanged.

## Explicit non-claims

The current engine is deterministic policy logic over supplied beliefs and forecasts. It does not call GPT, Claude, Gemini, a Chinese provider, a local model, or any network endpoint. Its seed and demo evidence are E0 simulated. No cost reduction, quality uplift, latency improvement, or superiority over another project may be claimed from this release alone.

## Upgrade gate

The next release may add calibrated capability evidence or real shadow adapters only after an independent, result-blind evaluation demonstrates positive Decision Regret/CPS improvement and planning overhead remains within the documented budget. Until then, the MetaController is advisory-only.
