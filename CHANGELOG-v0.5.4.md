# CostWeave v0.5.4

## Added

- Provider-neutral deployment identity and hierarchical deployment posteriors
- Cross-provider expected-cost receipt with predicted rework
- Pre-execution local load, VRAM, queue, deadline, bulkhead and circuit admission
- Economy, balanced, turbo and quality routing strategies
- Budget-aware quality/latency frontier repair before hard rejection
- Economics API and persisted Placement decision receipt
- migration 012 and Runtime / Projection Schema `v5.4`

## Validation

- 389 automated tests pass.
- Dedicated v0.5.4 tests cover deployment identity, expected-rework routing, local saturation, VRAM diversion, deadline P95, open circuits, quality mode, budget-aware quality rebalancing, posterior learning and receipt persistence.
- Existing cost, scheduler, migration, API and UI suites remain regression gates.

## Boundary

The release does not call paid model providers. It validates the control-plane contracts and deterministic simulated execution needed before real adapters are enabled.
