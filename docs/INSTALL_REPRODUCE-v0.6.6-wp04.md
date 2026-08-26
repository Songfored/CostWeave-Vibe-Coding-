# v0.6.6 V066-WP04 installation and reproduction

From the source root:

```text
python -B -m unittest -v tests.test_gateway_local_v066_wp04
python -B -m unittest -v tests.test_gateway_openai_v066_wp02 tests.test_gateway_litellm_v066_wp03 tests.test_gateway_local_v066_wp04 tests.test_release_v0584
python -B -m costweave.release_cli --pretty
```

The tests use a static capacity probe and an in-process fake server. They do
not require Ollama, vLLM, credentials or a network. A real local endpoint
requires explicit network and capacity policies plus a caller-owned fresh
telemetry probe.

The focused suite includes adversarial checks for contradictory timestamps,
future timestamps, delayed stream consumption and full-traceback probe error
redaction.
