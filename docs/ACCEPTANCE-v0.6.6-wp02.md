# v0.6.6 V066-WP02 acceptance

## Required gates

- Fake Server request and response wire contract passes without a socket.
- Provider-neutral message schema fields are not leaked to the provider body.
- Default policy rejects before transport and records zero fake requests.
- Remote endpoints, credentials, query strings and fragments fail closed.
- Usage, cached tokens, structured output and provider billing are mapped.
- HTTP errors become typed retryable/non-retryable gateway errors.
- Stream sequences are contiguous and include completion/error semantics.
- Cancellation never claims provider acceptance when unsupported.
- Existing v0.6.6 WP01 and prior regression suites remain green.

## Evidence boundary

This is E0 local/fake-server engineering evidence. It proves contract and
admission behavior only. It does not prove local model quality, throughput,
cost reduction, remote-provider compatibility or production readiness.

## Current result

WP02 focused tests cover the fake server, local endpoint admission, payload
mapping, structured output, usage, billing, streaming, errors and cancellation.
The WP02 focused suite passed **15/15**. The complete regression suite passed
**939 tests in 362.676 seconds**. Release CLI engineering gates remain green,
with runtime v6.5.0, migration 035, WAL and zero external-call attempts.
