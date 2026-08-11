# CostWeave v0.6.6 — V066-WP02 OpenAI-compatible Local Gateway

## Added

- Local-only OpenAI-compatible Gateway Backend.
- In-process Fake Server for deterministic wire-contract tests.
- Explicit network admission policy with safe default disabled state.
- Request payload mapping for messages, tools, temperature and structured
  output.
- Response mapping for usage, cached/reasoning tokens, provider billing header,
  provider request ID and HTTP error categories.
- Streaming event mapping and fail-closed cancellation behavior.
- Health policy disclosure for the local gateway.

## Not included

Remote endpoints, LiteLLM, paid calls, automatic retries, distributed
cancellation, model discovery, telemetry export and production Shadow remain
out of scope.
