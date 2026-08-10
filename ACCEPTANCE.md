# CostWeave v0.6.3 acceptance record

This file records the final implementation run and release build.

- Version: `0.6.3`
- Runtime schema: `v6.3.0`
- Migration: `033_joint_plan_route_schedule_search.sql`
- Scope: offline single-process advisory joint search
- Provider calls: `0`
- Network calls: `0`
- Commercial claims: disabled
- Production route intervention: disabled

## Results

- Focused v0.6.3 tests: `11 passed`
- Full regression: **881 tests, 0 failures, 351.05 seconds**
- Release readiness: `success=true`, `engineering_ready=true`
- Fresh Wheel import: `costweave 0.6.3`
- Fresh runtime: WAL, `v6.3.0`, **33 migrations**
- Offline CLI: `SELECT` on the bundled fixture; Provider/network calls `0`
- Archive hygiene: Wheel, runtime archive and source archive contain no
  `__pycache__`, `.pyc`, `.sqlite3`, `.env` or secret-named files

Acceptance must record:

1. focused v0.6.3 contract/API tests;
2. complete regression count and failure count;
3. release readiness gates;
4. independent Wheel import and CLI smoke;
5. SHA-256 and archive hygiene results.

The E0 fixture is evidence of deterministic engineering behavior only. It is
not evidence of real cost reduction, real model quality or competitive
superiority.
