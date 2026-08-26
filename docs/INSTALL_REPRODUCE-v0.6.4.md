# v0.6.4 install and reproduction

Install the wheel in an isolated environment:

```text
python -m pip install dist/costweave-0.6.4-py3-none-any.whl
python -m costweave.execution_recovery_cli --pretty
```

The CLI is deterministic and offline. It should report version `0.6.4`, select
`SWITCH_MODEL`, report a positive fixture-only recovery advantage, and show
`provider_calls=0` and `network_calls=0`.

A fresh runtime should use WAL, runtime schema `v6.4.0` and 34 migrations.
