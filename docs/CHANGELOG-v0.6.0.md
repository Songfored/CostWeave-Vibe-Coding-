# CostWeave v0.6.0 — Decision Intelligence Lab

`v0.5.8.4` remains the frozen stable baseline. `v0.6.0` is a separate development line built on top of that baseline; it does not rewrite migrations 001–029 or the historical v0.5 release artifacts.

## Added

- Strict task/provider/system belief, capability posterior, strategy DAG, decision trace, and outcome-credit contracts.
- Immutable decision-object and tournament-run persistence in migration 030.
- Result-blind routing benchmark contracts, a 36-record E0 seed dataset, safe JSONL/CSV importers, and external trace validation.
- Fixed, deterministic-random, static task-family, and recorded-decision policies.
- Deterministic metrics, paired bootstrap intervals, evidence/claim gates, and an offline tournament runner.
- `costweave-tournament` CLI and loopback-only offline tournament API.
- A small Decision Intelligence Lab section in the web UI.

## Safety boundary

- The bundled dataset is synthetic E0 evidence and cannot support a commercial superiority or cost-reduction claim.
- The tournament runner never calls a provider or network endpoint.
- Policies receive a blind `CaseViewV1`; actual quality, cost, latency, acceptance, prompt, and prediction fields are available only after the decision for evaluation.
- The v0.5.8.4 stable compatibility label remains visible alongside the v0.6.0 development label.
