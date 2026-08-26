# v0.6.6 V066-WP03 acceptance

## Required gates

- LiteLLM is represented as an injected external Gateway, not a copied SDK.
- Default policy rejects before a transport call.
- Paid and unknown-cost models require explicit paid-call authority and are
  rejected before transport when it is absent.
- Explicitly free models remain callable with paid calls disabled.
- Editable model aliases map deterministically and do not alter the core
  request contract.
- Usage, cached tokens, reasoning tokens, structured output, provider request
  IDs and optional response cost are mapped.
- HTTP errors become typed retryable/non-retryable gateway errors.
- Unexpected transport exceptions are never classified as retryable network
  failures.
- Release readiness includes LiteLLM gateway and paid-call default-off gates.
- Streaming and cancellation are explicitly deferred rather than falsely
  advertised.
- Existing WP01/WP02 and prior regression suites remain green.

## Evidence boundary

This increment is E0 local/fake-gateway engineering evidence. It proves
contract, mapping and admission behavior only. It does not prove real LiteLLM
provider quality, throughput, cost reduction or production readiness.

## Current result

- Corrected WP03 focused suite: 11/11 passed.
- Security/cost and gateway/server compatibility suite: 75/75 passed in
  56.765 seconds.
- Complete corrected regression suite: 951/951 passed in 331.132 seconds.
- Clean-target wheel install and release gates are recorded in the independent
  WP03 acceptance report.
