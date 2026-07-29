# CostWeave v0.5.5

## Added

- Provider-neutral HandoffCompiler and versioned WorkOrder
- Minimum Context Manifest with content hashes and no conversation copying
- Receiver preflight for plan, dependency, Artifact, Schema, permission, tool, modality, context, output and approval contracts
- Evidence-based Pcomplete checkpoints with risk-specific thresholds
- Hard/validation pollution propagation, soft suspicion and validated sibling preservation
- Bounded adaptive recovery with one global replan, authority preservation and fresh irreversible-action approval
- Handoff and recovery APIs plus a non-technical recovery-control card
- migration 013 and Runtime / Projection Schema `v5.5`

## Validation

- 402 automated tests pass.
- 13 dedicated v0.5.5 tests cover minimum context, preflight rejection, compatibility, Pcomplete, pollution, recovery limits, approval, authority preservation and persistence.
- The previous 389 tests remain regression gates.

## Boundary

The release uses deterministic simulated execution and does not call paid providers or perform real irreversible actions. Pcomplete and recovery thresholds are engineering defaults, not production-calibrated commercial claims.
