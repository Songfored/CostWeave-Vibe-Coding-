import unittest

from costweave.adapters import (
    GatewayBackend,
    GatewayBackendDisabledError,
    GatewayMessageV1,
    GatewayRequestV1,
    LiteLLMGatewayBackend,
    LiteLLMGatewayConfig,
    LiteLLMGatewayError,
    LiteLLMRuntimePolicy,
    FakeLiteLLMServer,
    public_litellm_gateway_policy,
)


class LiteLLMGatewayTests(unittest.TestCase):
    def setUp(self):
        self.config = LiteLLMGatewayConfig(
            backend_id="litellm-local-gateway",
            model_aliases={"planner": "openai/gpt-4o-mini"},
            model_billing_modes={"planner": "paid"},
        )
        self.request = GatewayRequestV1.create(
            trace_id="wp03-trace",
            project_id="wp03-project",
            model="planner",
            messages=(GatewayMessageV1(role="user", content="hello"),),
            max_output_tokens=32,
            idempotency_key="wp03-idem",
        )

    def test_default_policy_rejects_before_transport(self):
        fake = FakeLiteLLMServer()
        backend = LiteLLMGatewayBackend(self.config, transport=fake)
        with self.assertRaises(GatewayBackendDisabledError):
            backend.submit(self.request)
        self.assertEqual([], fake.calls)
        self.assertFalse(public_litellm_gateway_policy()["gateway_enabled"])

    def test_transport_is_required_even_when_policy_enabled(self):
        backend = LiteLLMGatewayBackend(
            self.config,
            policy=LiteLLMRuntimePolicy(
                gateway_enabled=True,
                paid_calls_enabled=True,
            ),
        )
        with self.assertRaises(GatewayBackendDisabledError):
            backend.submit(self.request)

    def test_fake_server_maps_alias_usage_and_cost(self):
        fake = FakeLiteLLMServer(
            content="mapped",
            response_cost=0.0025,
        )
        backend = LiteLLMGatewayBackend(
            self.config,
            policy=LiteLLMRuntimePolicy(
                gateway_enabled=True,
                paid_calls_enabled=True,
            ),
            transport=fake,
        )
        self.assertIsInstance(backend, GatewayBackend)
        result = backend.submit(self.request)
        self.assertEqual("completed", result.status)
        self.assertEqual("mapped", result.content)
        self.assertEqual("litellm-request-001", result.provider_request_id)
        self.assertEqual(11, result.usage.input_tokens)
        self.assertEqual(2, result.usage.cached_input_tokens)
        self.assertAlmostEqual(0.0025, result.billing.total_cost_usd)
        self.assertTrue(result.billing.reconciled)
        self.assertEqual("openai/gpt-4o-mini", fake.calls[0]["model"])
        self.assertNotIn("schema_version", fake.calls[0]["messages"][0])

    def test_unknown_model_is_preserved_and_structured_output_is_parsed(self):
        fake = FakeLiteLLMServer(content='{"ok":true}')
        backend = LiteLLMGatewayBackend(
            LiteLLMGatewayConfig(
                backend_id="litellm",
                model_aliases={},
                model_billing_modes={"ollama-llama3.2": "free"},
            ),
            policy=LiteLLMRuntimePolicy(gateway_enabled=True),
            transport=fake,
        )
        request = GatewayRequestV1.create(
            trace_id="structured",
            project_id="wp03-project",
            model="ollama-llama3.2",
            messages=(GatewayMessageV1(role="user", content="json"),),
            max_output_tokens=16,
            idempotency_key="structured-id",
            response_format={"type": "json_object"},
        )
        result = backend.submit(request)
        self.assertEqual({"ok": True}, result.structured_output)
        self.assertEqual("ollama-llama3.2", fake.calls[0]["model"])

    def test_http_errors_map_and_do_not_leak_provider_message(self):
        fake = FakeLiteLLMServer(status_code=429)
        backend = LiteLLMGatewayBackend(
            self.config,
            policy=LiteLLMRuntimePolicy(
                gateway_enabled=True,
                paid_calls_enabled=True,
            ),
            transport=fake,
        )
        result = backend.submit(self.request)
        self.assertEqual("rate_limit", result.error.category)
        self.assertTrue(result.error.retryable)
        self.assertNotIn("fixture error", result.error.message)

    def test_streaming_is_explicitly_deferred_and_cancellation_not_accepted(self):
        fake = FakeLiteLLMServer()
        backend = LiteLLMGatewayBackend(
            self.config,
            policy=LiteLLMRuntimePolicy(
                gateway_enabled=True,
                paid_calls_enabled=True,
            ),
            transport=fake,
        )
        stream_request = GatewayRequestV1.create(
            trace_id="stream",
            project_id="wp03-project",
            model="planner",
            messages=(GatewayMessageV1(role="user", content="hello"),),
            max_output_tokens=16,
            idempotency_key="stream-id",
            stream=True,
        )
        with self.assertRaises(LiteLLMGatewayError):
            tuple(backend.stream(stream_request))
        cancellation = backend.cancel(stream_request.request_id, reason="test")
        self.assertFalse(cancellation.accepted)

    def test_invalid_aliases_fail_closed(self):
        with self.assertRaises(LiteLLMGatewayError):
            LiteLLMGatewayConfig(
                backend_id="bad gateway",
                model_aliases={"planner": "https://provider.example/model"},
            )

    def test_paid_and_unknown_models_require_explicit_paid_authority(self):
        for config in (
            self.config,
            LiteLLMGatewayConfig(backend_id="unknown-billing"),
        ):
            fake = FakeLiteLLMServer(response_cost=0.01)
            backend = LiteLLMGatewayBackend(
                config,
                policy=LiteLLMRuntimePolicy(
                    gateway_enabled=True,
                    paid_calls_enabled=False,
                ),
                transport=fake,
            )
            with self.assertRaises(GatewayBackendDisabledError):
                backend.submit(self.request)
            self.assertEqual([], fake.calls)

    def test_free_model_does_not_require_paid_authority(self):
        fake = FakeLiteLLMServer(content="free", response_cost=None)
        backend = LiteLLMGatewayBackend(
            LiteLLMGatewayConfig(
                backend_id="free-gateway",
                model_billing_modes={"planner": "free"},
            ),
            policy=LiteLLMRuntimePolicy(
                gateway_enabled=True,
                paid_calls_enabled=False,
            ),
            transport=fake,
        )
        result = backend.submit(self.request)
        self.assertEqual("completed", result.status)
        self.assertEqual(1, len(fake.calls))

    def test_unexpected_transport_exception_is_not_retryable(self):
        class BrokenTransport:
            def complete(self, *, payload):
                del payload
                raise ValueError("programming bug")

        backend = LiteLLMGatewayBackend(
            self.config,
            policy=LiteLLMRuntimePolicy(
                gateway_enabled=True,
                paid_calls_enabled=True,
            ),
            transport=BrokenTransport(),
        )
        result = backend.submit(self.request)
        self.assertEqual("failed", result.status)
        self.assertEqual("unknown", result.error.category)
        self.assertFalse(result.error.retryable)
        self.assertNotIn("programming bug", result.error.message)

    def test_invalid_billing_mode_fails_closed(self):
        with self.assertRaises(LiteLLMGatewayError):
            LiteLLMGatewayConfig(
                backend_id="invalid-billing",
                model_billing_modes={"planner": "maybe-free"},
            )


if __name__ == "__main__":
    unittest.main()
