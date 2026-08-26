# V066-WP07 benchmark boundary

WP07 is a control-plane and budget-safety increment, not a model benchmark.
Its deterministic fixture verifies admission, project isolation, idempotency,
hard-budget behavior, bounded retention and failure isolation.

Current engineering evidence:

- 9/9 WP07 focused tests passed;
- 59/59 focused compatibility tests passed;
- 1011/1011 complete discovery tests passed;
- 18/18 release gates passed;
- real Provider, paid and network calls: 0;
- evidence level: E0 contract-and-fixture only.

The fixture's token, latency and USD values are synthetic and must not be used
to estimate savings or quality. WP08 must use frozen tasks, baseline routes,
Provider usage receipts, invoices, retry/cache facts, independent outcomes and
pre-registered metrics before comparing cost, latency or success rate.
