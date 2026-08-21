# V066-WP07 install and reproduction

WP07 adds no runtime dependency or database migration. From the source tree:

```text
python -B -m unittest -v tests.test_small_budget_shadow_v066_wp07
python -B -m unittest discover -s tests -p "test*.py"
python -B -m costweave.release_cli --pretty
```

The default server exposes:

```text
GET  /api/v1/runtime/shadow/status
POST /api/v1/runtime/shadow/experiments
GET  /api/v1/runtime/shadow/experiments/<experiment_id>
POST /api/v1/runtime/shadow/experiments/<experiment_id>/probes
```

Use `X-CostWeave-Project` consistently and supply `Idempotency-Key` for both
POST operations. Merely configuring an experiment does not make a call. The
standard build has no `ShadowProbeExecutor`; its experimental and paid switches
also default to off. A deliberate integration must inject an executor and grant
the required layered authority. Never place a secret value in JSON; for remote
configuration use only a secret environment reference.
