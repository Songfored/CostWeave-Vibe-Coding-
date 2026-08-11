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
- Candidate assessments remain advisory-only and route mutation is false.
- Provider, network and paid calls are zero by default.

## Evidence statement

The bundled demo is E0 simulated evidence. It intentionally produces a
rejected candidate. Real cost, quality, latency, savings and competitive
claims remain unverified until an independently reviewed E2+ benchmark is
available.
