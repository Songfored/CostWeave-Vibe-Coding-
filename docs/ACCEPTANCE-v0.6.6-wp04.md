# v0.6.6 V066-WP04 acceptance

## Required gates

- Local endpoint is loopback-only and rejects credentials, query strings and
  fragments.
- Network and capacity admission are default-off.
- Missing, invalid, incomplete and stale capacity telemetry fail closed before
  transport.
- Snapshot freshness is derived from `captured_at` as well as the reported age;
  contradictory stale or future timestamps fail closed.
- Queue depth, available slots and estimated wait are hard constraints.
- Admitted snapshots reach the existing OpenAI-compatible contract.
- Streaming performs the capacity check when iteration starts, immediately
  before provider I/O.
- Probe errors do not leak exception details through the full exception chain.
- Release readiness includes local network and local capacity default-off gates.
- No database migration and no external call are required by the fixture.

## Evidence boundary

The built-in tests use `StaticLocalCapacityProbe` and
`FakeOpenAICompatibleServer`; they are E0 engineering evidence. They do not
prove Ollama/vLLM metrics compatibility, GPU throughput, real queue prediction,
production concurrency, cost reduction or commercial superiority.
