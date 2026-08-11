# v0.6.5 benchmark and evidence boundary

The included deterministic demo reports the following engineering facts:

- six-layer credit is deterministic and round-trippable;
- counterfactual replay preserves the delta identity;
- IPS and DR produce bounded estimates and effective sample size;
- E0 fixture estimates are rejected by the learning gate;
- SQLite rows are immutable, project-scoped and idempotent;
- Provider calls and network calls remain zero.

These are contract tests, not an end-to-end quality benchmark. No real
success-rate, cost-reduction, latency or competitive-superiority number is
claimed by this release. A real benchmark must preregister cohorts, logging
policy, propensity coverage, labels, cost ledger, latency clock, Validator
independence and rollback criteria.
