# CostWeave v0.6.6 release acceptance report

## Scope

V066-WP01 Gateway Backend Contract only. This report accepts the provider-
neutral request/event/response boundary and its offline regression evidence.
It does not accept a real provider integration or production deployment.

## Verification

- Focused suite: 60 passed.
- Full suite: 924 passed in 321.691 seconds.
- Release CLI: engineering gates passed.
- Runtime schema: v6.5.0; migrations: 35; journal: WAL.
- External-call attempts: 0; paid calls: 0; network: disabled by default.
- Package version: 0.6.6.

## Artifacts

The release directory contains the wheel, source archive, runtime archive and
SHA-256 manifest. Hashes in the manifest are generated after the final files
are assembled.

## Evidence boundary

This is E0 local verification. It supports contract and regression-health
claims only. It does not support provider quality, cost reduction, latency,
high-concurrency capacity, production readiness or commercial superiority.
