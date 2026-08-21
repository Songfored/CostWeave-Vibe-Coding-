# V066-WP07 data card

## Inputs

Experiment configuration accepts explicit Provider/model/deployment, data and
residency scope, hard budgets, call/token limits, expiry and consent. A probe
accepts a validated text-only, tool-free WorkOrder plus the immutable baseline
route ID/fingerprint and a caller idempotency key.

## Retained outputs

The durable ledger may retain status, bounded token counts, cached-input token
count, latency, actual USD cost, evidence level and an optional SHA-256 output
hash. It retains budget reservations and safe telemetry stating that the
baseline continued and no route changed.

When execution fails without trustworthy cost telemetry, the ledger records the
reserved per-probe ceiling as a conservative cost. This is an upper-bound
accounting control, not a reconciled Provider bill.

## Excluded data

Raw WorkOrders, prompts, model outputs, credentials and secret environment
names/values are excluded. `metrics_only` forbids an output hash;
`metrics_and_output_hash` requires one. Public configuration reveals only a
boolean that a secret reference was configured.

## Scope and provenance

Every active experiment is project-scoped and expires within 24 hours. Built-in
records come from an E0 deterministic fixture, not user production traffic,
training data, a Provider bill or independently judged model output.
