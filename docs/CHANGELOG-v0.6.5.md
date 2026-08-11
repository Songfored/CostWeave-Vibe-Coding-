# CostWeave v0.6.5 — Trace Credit Assignment & Safe Learning

## What changed

- Added a strict trace-credit contract for decomposition, routing, handoff,
  execution, validation and recovery.
- Added deterministic single-node counterfactual replay.
- Added logged-policy IPS and doubly-robust (DR) estimates with propensity,
  common-support, calibration and effective-sample-size gates.
- Added immutable, project-scoped SQLite persistence and idempotency for
  credits, replays, estimates, candidates and promotion assessments.
- Added Champion/Challenger candidate records, rollback references and
  offline/shadow/canary assessment states.
- Added a loopback API, CLI demo and UI panel.

## Safety boundary

v0.6.5 never trains a model, changes a live route, calls a Provider or accesses
the network. E0 fixtures are rejected for learning and cannot support cost,
quality, latency or commercial claims. A candidate can only become
shadow-eligible after E2+ independently validated real observations and all
hard gates pass; even then, this release records an advisory recommendation
only.

## Migration

The runtime schema moves from v6.4.0 to v6.5.0. Migration 035 is append-only
and does not modify historical migrations.
