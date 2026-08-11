# CostWeave v0.6.5 acceptance report

Date: 2026-08-11

## Result

PASS for the offline engineering scope. The runtime is advisory-only and
remains unsuitable for production learning or commercial claims.

## Verification

- Focused v0.6.5 contract/API/UI tests: 18 passed in 2.426 seconds.
- Full regression suite: 907 passed in 328.573 seconds.
- Release readiness: engineering_ready=true, production_ready=false.
- Runtime: v6.5.0, migration count 35, SQLite WAL, seven release gates passed.
- Safe-learning demo: E0 simulated, assessment=rejected,
  training_enabled=false, route_intervention=false.
- Provider calls: 0. Network calls: 0. Paid calls: 0.

## Artifacts

- Wheel: costweave-0.6.5-py3-none-any.whl
  SHA-256 e99aa5131c44de87c0140cda3aea17a22e4fbf868f7f6e55f5a98367fe78eb7d
- Runtime archive: costweave-v0.6.5-final.zip
  SHA-256 0ee1b398bb56a2e69343fe25dc7880506b8771eaec5731680586cb7547d02e62
- Source archive: costweave-v0.6.5-source-final.zip
  SHA-256 0126fb88a905797dac10f8802ac59be494cb45ed6a74144cda1f29e4637ffcc7

## Boundary

The demo proves deterministic contracts, persistence, idempotency and
fail-closed gates only. It does not prove real model quality, savings,
latency, high-concurrency capacity or superiority over another system.
