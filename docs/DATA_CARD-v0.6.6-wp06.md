# V066-WP06 data card

## Inputs

The submit contract accepts a user goal, project and data-residency scope,
bounded execution limits, a caller idempotency key and optional metadata.
Checkpoint, resume and cancel accept a run ID, project scope, expected runtime
revision and an idempotency key.

## Outputs

Operations return a run snapshot, status, revision, Trace ID and replay flag.
Event pages return ordered durable event metadata and sanitized payloads.

## Protection

Project scope is checked before reads and writes. Metadata and event payloads
are finite-JSON validated and passed through the observability sanitizer.
Secrets and prompt-like values are rejected or replaced by metadata-only
markers. Runtime request metadata is not used as a model prompt.

## Evidence classification

All built-in WP06 records are E0 local engineering fixtures. They are not
training data, real provider billing, real user production traffic or evidence
for a cost-reduction claim.
