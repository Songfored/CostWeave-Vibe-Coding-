# CostWeave v0.6.2 acceptance record

The release acceptance was executed in the workspace on 2026-08-09.

- Runtime schema: `v6.2.0`
- Migration: `032_capability_evidence_graph.sql`
- Full isolated regression: **870 tests passed, 0 failed** (`328.60s`)
- Fresh wheel install: `costweave-0.6.2-py3-none-any.whl`, import version `0.6.2`
- Fresh install health: WAL, runtime `v6.2.0`, **32 migrations**
- Offline CLI smoke: capability evidence → calibrated posterior → `ROUTE`
- Release CLI: `success=true`, `engineering_ready=true`, event replay and backup verification passed
- Evidence level of bundled demo: `E0-simulated`
- Provider/network calls: **0**
- Commercial claims: `false`

## Published artifacts

- `outputs/costweave-v0.6.2.zip`
- `outputs/costweave-v0.6.2-source.zip`
- `outputs/v062-dist/costweave-0.6.2-py3-none-any.whl`

SHA-256 values are published separately in
`outputs/costweave-v0.6.2-SHA256SUMS.txt` to avoid making the archive hash
self-referential.

These are engineering-contract results, not evidence of real model quality,
cost reduction, throughput or competitive superiority.
