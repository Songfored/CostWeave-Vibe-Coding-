# v0.6.3 install and reproduction

```powershell
python -m pip install dist/costweave-0.6.3-py3-none-any.whl
python -m costweave.joint_search_cli --pretty
python -m costweave.release_cli --pretty
```

The CLI is deterministic and offline. It should report `version=0.6.3`,
`action=SELECT` for the built-in fixture, `provider_calls=0` and
`network_calls=0`. A fresh runtime should use WAL, runtime schema `v6.3.0`
and exactly 33 migrations.
