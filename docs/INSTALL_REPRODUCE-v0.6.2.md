# v0.6.2 install and reproduction

Use Python 3.11 or newer. The source package has no required third-party
runtime dependency.

```text
python -m pip install dist/costweave-0.6.2-py3-none-any.whl
python -m costweave.capability_router_cli --pretty
python -m costweave.release_cli --pretty
```

The CLI creates a temporary SQLite database, applies migration 032, ingests an
E0 fixture and emits a deterministic advisory route. It performs zero Provider
and network calls. For a server, bind only to loopback and use an explicit
runtime database path; API writes require `Idempotency-Key` and the confirmation
token `RUN_CAPABILITY_ROUTER`.
