# CostWeave v0.6.4 acceptance record

This record is completed after the implementation, focused tests, full
regression, release gate, isolated Wheel install and archive hygiene checks.

- Scope: offline single-process execution-time belief and economic recovery;
- Production route intervention: disabled;
- Provider/network calls: 0;
- Commercial claims: disabled;
- Evidence level: E0 local verification.

## Revalidation results

- Core execution-recovery contracts and API: `12 passed`;
- UI regression checks: `4 passed`;
- Full regression suite: `893 passed in 339.701s`;
- Release gate: `success=true`, `engineering_ready=true`,
  `production_ready=false`;
- Runtime: SQLite WAL, schema `v6.4.0`, `34` migrations;
- Offline CLI: selected `SWITCH_MODEL`, provider calls `0`, network calls `0`;
- Fault-injection fixture: dynamic CPS `3.4947696061`, fixed retry CPS
  `5.8339608771`, fixed full-replan CPS `4.0965841626`, recovery advantage
  `0.60181456`.

The full suite was executed with `python -B -m unittest discover -v`.
The focused v0.6.4 checks were executed separately before the release gate.

The fixture benchmark is intentionally not a real cost, quality or latency
claim. Archive and wheel SHA-256 values are recorded in
`outputs/costweave-v0.6.4-SHA256SUMS.txt` after packaging.
