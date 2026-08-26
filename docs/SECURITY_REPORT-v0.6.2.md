# v0.6.2 security boundary report

## Verified

- Capability graph writes use the existing SQLite EventStore database with WAL,
  foreign keys, immutable triggers and SHA-256 content checks.
- Evidence and decisions are project-scoped; idempotency keys cannot cross
  project boundaries or be reused for different content.
- Raw prompts, outputs and secrets are not accepted by the v0.6.2 contracts.
- API and CLI paths are offline by default and report zero Provider/network
  calls. Route decisions are advisory-only.

## Not verified

- physical erasure of all SQLite pages;
- distributed concurrency or multi-process deployment;
- real Provider authentication, billing or model quality;
- production failover and online policy promotion.
