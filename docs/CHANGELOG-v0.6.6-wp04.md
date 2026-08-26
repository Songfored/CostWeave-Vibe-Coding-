# CostWeave v0.6.6 — V066-WP04 Local Endpoint Capacity Admission

## Added

- `LocalEndpointConfig` for generic, Ollama and vLLM-style loopback endpoints.
- `LocalCapacitySnapshot`, `LocalCapacityProbe` and static E0 probe contract.
- `LocalEndpointAdmissionPolicy` with default-off network/capacity controls.
- `LocalOpenAICompatibleGatewayBackend` wrapping WP02 with pre-execution
  queue, slot, wait and telemetry-freshness admission.
- Release gates for local endpoint network and capacity default-off behavior.
- WP04 installation, acceptance, benchmark, policy, model, data and security
  cards.

## Boundaries

No database migration, metrics SDK, hardware discovery, remote endpoint,
production queue reservation, Shadow traffic or commercial claim is added.

## Acceptance repairs (2026-08-13)

- Derive effective telemetry age from the trusted local clock and
  `captured_at`; reject stale and future snapshots.
- Move streaming admission to iterator execution so delayed consumption cannot
  use an earlier capacity decision.
- Remove untrusted probe exceptions from the exported exception chain.
- Make complete telemetry a non-disableable invariant.
- Add focused regression tests for all four repaired boundaries.
