# CostWeave v0.6.3 — Joint Plan–Route–Schedule Search

v0.6.3 is an offline, provider-free advisory release. It searches a complete
execution strategy instead of making an independent model choice for each
task node.

## Added

- deterministic candidate factory with safety baseline, historical preference,
  planner preference and bounded local transforms;
- joint management topology, DAG, route, reasoning tier, tools, concurrency,
  validation and recovery scoring;
- hard budget/deadline/chance/authority/tool constraints;
- Pareto retention, greedy, beam and successive-halving searchers;
- CVaR-like tail-loss penalty and amortised planning cost;
- Anytime return of the best feasible strategy found within the budget;
- Prompt/Handoff compiler checks for deliverable coverage, shared-field
  contradiction, authority, tool scope and information loss;
- immutable project-scoped candidate/decision persistence, idempotency and
  API/CLI/UI surfaces;
- migration 033 and runtime schema `v6.3.0`.

## Safety boundary

The joint search result is `advisory_only=true`. It does not change the stable
execution route, call a Provider, access a network or enable training. Built-in
fixtures are `E0-simulated`; no cost, quality, latency or competitive claim is
made.

## Verification

The release must pass the full regression suite, the v0.6.3 focused contracts,
the release readiness gate, independent Wheel installation and archive
hygiene checks before publication.
