# v0.6.6 V066-WP03 installation and reproduction

From the source root:

```text
python -B -m unittest -v tests.test_gateway_litellm_v066_wp03
python -B -m unittest -v tests.test_gateway_backend_v066 tests.test_gateway_openai_v066_wp02 tests.test_gateway_litellm_v066_wp03
python -B -m costweave.release_cli --pretty
```

The LiteLLM tests do not require the LiteLLM package, an API key or a network.
They use `FakeLiteLLMServer`, which is an in-process fixture. A real gateway
must be explicitly injected and separately authorized by the caller.
