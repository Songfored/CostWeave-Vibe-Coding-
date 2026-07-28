# CostWeave v0.5.1

## Added

- Provider-neutral planning contracts and six planning modes.
- Fixed two-level MetaPlanner / Planner management boundary.
- Domain, economics and risk planner roles with catalog-based assignment.
- Deterministic PlanningDecision fingerprints and proposal receipts.
- WorkOrder and ResultEnvelope handoff contracts.
- ProviderAdapter interface and zero-paid-call MockProvider.
- Planning policy and per-run planning APIs.
- MetaPlanner explanation card in the web console.
- Runtime migration 009 and v4.9 projection compatibility.

## Changed

- Product version is now 0.5.1.
- Runtime and Projection Schema are now `v5.1`.
- Every newly planned run persists a deliberation receipt before DAG planning.
- The existing heuristic planner can reuse an already computed TaskAnalysis.

## Safety and scope

- No real model API is called.
- Paid calls are disabled by contract.
- The v0.5.1 selector is a deterministic simulator, not the learned Planning Governor planned for v0.5.2.

