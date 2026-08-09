# v0.6.1 install and reproduction

From a clean Python environment in the repository root:

```text
python -m pip install .
costweave-metacontroller --pretty
python -B -m unittest tests.test_meta_controller_v061 tests.test_meta_controller_api_v061
costweave-release --pretty
```

The CLI demo prints a `fast` decision with `provider_calls: 0` and `network_calls: 0`. The API requires a loopback server and an `Idempotency-Key`; it persists only to the shared SQLite runtime. No API key or network access is required.

For a clean database, start the server with a temporary catalog path or call `build_server(..., runtime_db_path=...)`. The first startup applies migration 031 and reports runtime schema `v6.1.0` with 31 migrations. Do not edit an applied migration file; use a new migration for future schema changes.
