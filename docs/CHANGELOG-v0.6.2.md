# CostWeave v0.6.2 — Capability Evidence Graph & Calibrated Router

`v0.5.8.4` remains the frozen stable compatibility baseline. `v0.6.0` and
`v0.6.1` remain historical, provider-free decision-intelligence lines. v0.6.2
adds an additive capability evidence graph and a conservative calibrated route
adviser. It does not activate providers, network access, training, Shadow
intervention or commercial claims.

## Added

- Heterogeneous graph contracts for task family, model revision, reasoning tier,
  tool, modality, provider deployment and validator nodes.
- Immutable, project-scoped evidence records with source kind, source version,
  selection mechanism, evidence level, sample counts, costs, latency, retries,
  escalation, rework and model/validator failure correlation.
- Decayed Beta-style calibration with wide cold-start priors, effective sample
  size, coverage, stale evidence and OOD status.
- Conservative `ROUTE`, `ABSTAIN` and `ESCALATE` decisions with complete route
  assessments and trace hashes.
- Drift summary that freezes candidate promotion when task, model, price,
  latency or error-rate drift crosses the configured boundary.
- Offline CLI and loopback API endpoints:
  `GET /api/v1/capability-router/status`,
  `POST /api/v1/capability-router/evidence`,
  `POST /api/v1/capability-router/decide`, and
  `GET /api/v1/capability-router/{decision_id}`. Drift summaries are available
  through `POST /api/v1/capability-router/drift` and never promote a strategy.

## Explicit non-claims

The bundled fixture is `E0-simulated`. It is useful for contract and replay
tests only. v0.6.2 does not claim a cost reduction percentage, model quality,
production readiness, calibration on real traffic or superiority over any
competitor.
