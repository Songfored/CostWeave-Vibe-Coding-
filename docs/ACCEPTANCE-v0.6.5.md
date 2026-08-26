# CostWeave v0.6.5 acceptance

## Scope

This release accepts the trace-credit assignment and safe-learning contracts,
immutable persistence, API, CLI and UI wiring. It is an engineering release,
not a production-learning release.

## Required gates

- Runtime schema v6.5.0 and migration 035 are applied with checksums.
- Six component credit is deterministic and round-trippable.
- E0 and E1 data cannot pass the learning gate.
- IPS and DR reject missing support, propensity, calibration or sample floors.
- Every positively weighted candidate action has logging-policy support.
- Raw importance weights above 20 fail closed even though calculations remain capped.
- Mixed-project observations are rejected before estimation.
- Evaluation records and their idempotency binding commit or roll back together.
- Idempotent evaluation replay returns the complete estimate, candidate and assessment.
- Candidate assessments remain advisory-only and route mutation is false.
- Provider, network and paid calls are zero by default.

## Repair verification

- Focused safe-learning, API, migration, release and UI tests: 47 passed.
- Full regression suite: 911 passed in 324.621 seconds.
- A database-trigger fault injected during assessment persistence left zero
  estimate, candidate, assessment and idempotency rows.

## Evidence statement

The bundled demo is E0 simulated evidence. It intentionally produces a
rejected candidate. Real cost, quality, latency, savings and competitive
claims remain unverified until an independently reviewed E2+ benchmark is
available.
