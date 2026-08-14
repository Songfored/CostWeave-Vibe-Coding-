# CostWeave v0.6.6 — V066-WP06 release acceptance

## Result

**Accepted for the E0 local-verification scope.** This release adds the
local-first Runtime Backend contract and keeps third-party runtimes, paid
provider calls, network calls, and external telemetry disabled by default.

## Verified evidence

| Area | Result |
|---|---:|
| WP06 focused contract/API tests | 7/7 passed |
| Complete unittest discovery | 1002/1002 passed |
| Release-readiness gates | 17/17 passed |
| External/paid provider calls | 0 |
| Runtime schema | v6.5.0 |
| Runtime migrations | 35 |
| SQLite journal mode | WAL |
| Release evidence level | E0-local-verification |

The focused tests cover strict request validation, secret-bearing metadata
rejection, local submit/cancel/checkpoint/resume/events, project isolation,
HTTP status contracts, idempotent replay, changed-command rejection, and the
checkpoint/resume operation-identity boundary.

## Included surfaces

- `costweave.runtime_backend`: versioned request, capability, operation,
  event-page, and adapter protocol contracts;
- `LocalRuntimeBackend`: persistent single-process implementation over the
  existing `RunManager`/`ControlKernel`;
- `/api/v1/runtime/status`, `/submit`, per-run snapshot, events, cancel,
  checkpoint, and resume endpoints;
- release self-test coverage for the runtime backend contract;
- implementation, migration, security, policy, data, benchmark, and
  installation documentation under `docs/`.

## Packaging

The release artifacts are generated from the repository after the acceptance
run:

- `costweave-v0.6.6-wp06-py3-none-any.whl` (built wheel filename inside is
  `costweave-0.6.6-py3-none-any.whl`);
- `costweave-v0.6.6-wp06-runtime-release.zip` (runtime package, no tests);
- `costweave-v0.6.6-wp06-source-release.zip` (source package with tests);
- `SHA256SUMS-v0.6.6-wp06.txt`.

Hashes are recorded in the adjacent SHA256 manifest after packaging.

## Explicit non-claims

This evidence does not claim real-provider answer quality, real cost or
latency reduction, distributed-worker availability, GPU capacity, production
recovery, external telemetry delivery, or commercial superiority. Those
remain future benchmark and integration work.
