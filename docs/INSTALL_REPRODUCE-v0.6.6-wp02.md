# v0.6.6 V066-WP02 installation and reproduction

From the source root:

```text
python -B -m unittest -v tests.test_gateway_openai_v066_wp02
python -B -m unittest -v tests.test_gateway_backend_v066 tests.test_gateway_openai_v066_wp02
python -B -m costweave.release_cli --pretty
```

The Fake Server tests do not require an API key, a local model process or a
network. To use a real loopback endpoint, construct an explicit local config
and pass `OpenAICompatibleRuntimePolicy(network_enabled=True)`; remote URLs are
rejected by design.
