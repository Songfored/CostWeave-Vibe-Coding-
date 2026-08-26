# V066-WP06 install and reproduction

The package has no new runtime dependency or migration. From the source tree:

```text
python -m unittest -v tests.test_runtime_backend_v066_wp06
python -m unittest discover -s tests -p "test*.py"
python -m costweave.release_cli --pretty
```

The HTTP contract is exercised by the focused suite. To inspect it manually,
start the existing loopback server and call:

```text
GET  /api/v1/runtime/status
POST /api/v1/runtime/submit                 (Idempotency-Key required)
POST /api/v1/runtime/<run_id>/checkpoint    (key + expected_revision)
POST /api/v1/runtime/<run_id>/resume        (key + expected_revision)
GET  /api/v1/runtime/<run_id>/events?limit=200
POST /api/v1/runtime/<run_id>/cancel        (key + expected_revision)
```

Use `X-CostWeave-Project` consistently. The default installation remains
loopback-only and makes zero provider or network calls.
