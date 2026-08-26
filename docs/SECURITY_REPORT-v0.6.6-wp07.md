# V066-WP07 security report

## Threats addressed

- accidental paid or network execution;
- stale consent automatically re-arming after restart;
- duplicate calls caused by client retries;
- overspend from concurrent admission or automatic retries;
- cross-project experiment disclosure;
- raw prompt/output/credential retention;
- prompt-injection content entering a live Shadow executor;
- unbounded output or non-finite accounting values;
- Shadow failure changing the production route; and
- fixture evidence being misrepresented as production proof.

## Controls

Default global switches are off and the default server has no executor.
Remote admission requires layered consent and current secret availability.
The existing SQLite budget owner performs atomic reservation before transport;
experiment and probe keys are fingerprinted for idempotent replay. WP07 permits
one attempt only, and an ambiguous executor failure consumes its reserved
ceiling rather than reopening that budget. Secret-reference identity is folded
into the configuration fingerprint without exposing the reference. Project
scope is checked before reads/execution. Strict JSON, secret-pattern and
prompt-security checks run at the boundary. Only bounded metrics and an
optional one-way output hash are retained. Receipts hard-code advisory-only,
unchanged-route and no-promotion invariants.

## Residual risk

Environment variables are not a complete secret-management system, project
headers are not authentication and an injected executor remains trusted code.
A Provider can bill a request that times out before a receipt arrives; WP07
isolates and does not retry it, but cannot reconcile that bill. The in-process
configuration store is intentionally not distributed. Real data-residency,
Provider terms, invoice reconciliation, multi-process races and production
traffic require separate validation in WP08 or later.
