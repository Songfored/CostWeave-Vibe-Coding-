# CostWeave v0.6.6 — V066-WP07 release acceptance

## Result

**Accepted for the E0 local-verification scope.** This release adds an
explicitly armed, hard-budgeted Small-Budget Shadow control plane while keeping
the built-in executor absent and network/paid calls disabled by default.

## Verified evidence

| Area | Result |
|---|---:|
| WP07 focused contract/runtime/API tests | 9/9 passed |
| WP06/WP05/server/release compatibility subset | 59/59 passed |
| Complete unittest discovery | 1011/1011 passed |
| Release-readiness gates | 18/18 passed |
| Real external/paid Provider calls | 0 |
| Runtime schema | v6.5.0 |
| Runtime migrations | 35 |
| SQLite journal mode | WAL |
| Release evidence level | E0-local-verification |

The focused tests cover strict bounded configuration, public secret redaction,
project isolation, data-residency conflicts, disabled-before-reservation
behavior, remote secret availability, atomic budget admission, one-call replay,
conservative failure accounting, call-limit exhaustion, secret-bearing prompt
rejection, output-token overrun isolation and optional-runtime health behavior.

## Included surfaces

- `costweave.small_budget_shadow`: versioned experiment, probe, measurement,
  receipt and executor contracts;
- `SmallBudgetShadowRuntime`: project-scoped sessions over the existing durable
  experimental-call budget ledger;
- `/api/v1/runtime/shadow/status`, experiment configure/inspect and probe APIs;
- release self-test coverage for disabled-by-default, advisory-only and hard
  upper-bound policy invariants; and
- implementation, migration, security, policy, data, benchmark and
  installation documentation under `docs/`.

## Packaging

The release artifacts are generated from the repository after the acceptance
run:

- `costweave-0.6.6-py3-none-any.whl` (standards-compliant wheel filename; WP07
  identity is recorded in this acceptance report and the adjacent archives);
- `costweave-v0.6.6-wp07-runtime-release.zip` (runtime package, no tests);
- `costweave-v0.6.6-wp07-source-release.zip` (source package with tests);
- `SHA256SUMS-v0.6.6-wp07.txt`.

Hashes are recorded in the adjacent SHA256 manifest after packaging.

## Explicit non-claims

This evidence does not claim real-Provider answer quality, reconciled billing,
real cost or latency reduction, production Shadow traffic, distributed
execution, automatic route improvement or commercial superiority. The fixture
metrics are synthetic; V066-WP08 remains responsible for real reconciliation.
