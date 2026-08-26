# v0.6.6 V066-WP03 security report

WP03 keeps credentials and provider SDKs outside the core. The default policy
blocks the transport before any call. Configuration accepts only bounded model
aliases; payloads are size bounded; malformed provider usage, costs and
responses fail closed; provider error text is not copied into the core error.

Residual scope: an explicitly injected external transport remains the caller's
security boundary and must provide its own authorization, network isolation,
TLS and audit controls.

## 2026-08-13 security correction

The original WP03 artifact exposed `paid_calls_enabled` without enforcing it
in admission and classified every unexpected transport exception as retryable
network failure. The corrected boundary adds explicit `free|paid|unknown`
billing modes, treats missing/unknown as paid, rejects unauthorized paid calls
before transport, and makes unclassified exceptions non-retryable. LiteLLM
gateway and paid-call defaults are also included in release readiness gates.
