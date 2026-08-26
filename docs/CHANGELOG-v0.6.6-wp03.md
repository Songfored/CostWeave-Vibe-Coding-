# CostWeave v0.6.6 — V066-WP03 LiteLLM Gateway Interop

## Added

- `LiteLLMGatewayBackend` implementing the provider-neutral Gateway Backend.
- Editable bounded model aliases without provider-specific logic in the core.
- Explicit injected transport contract and deterministic `FakeLiteLLMServer`.
- Mapping for LiteLLM-style model IDs, usage, cached/reasoning tokens, optional
  response cost, structured output, request IDs and typed HTTP errors.
- Health policy reporting for the LiteLLM boundary.

## Safety

- LiteLLM SDK import, credentials, network calls and paid calls remain disabled
  unless a caller explicitly supplies and enables a transport.
- Streaming and provider cancellation remain deferred and are not advertised.

## Evidence

This is E0 local/fake-gateway engineering evidence. It does not establish real
provider quality, production readiness, compatibility coverage or cost savings.

## Security correction

- Enforced `paid_calls_enabled` with explicit model billing modes.
- Defaulted missing model billing evidence to `unknown` and paid authorization.
- Limited retryable network classification to `LiteLLMTransportError`.
- Added LiteLLM default-off gates to release readiness.
