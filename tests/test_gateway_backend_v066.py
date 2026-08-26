import unittest

from costweave.adapters import (
    GATEWAY_SCHEMA_VERSION,
    GatewayBackend,
    GatewayBackendContractError,
    GatewayBillingV1,
    GatewayCapabilitiesV1,
    GatewayCancellationV1,
    GatewayErrorV1,
    GatewayMessageV1,
    GatewayRequestV1,
    GatewayResponseV1,
    GatewayStreamEventV1,
    GatewayToolV1,
    GatewayUsageV1,
    public_gateway_policy,
    validate_response_for_request,
)


class GatewayBackendContractTests(unittest.TestCase):
    def setUp(self):
        self.message = GatewayMessageV1(role="user", content="Summarise the report")
        self.tool = GatewayToolV1(
            name="lookup",
            description="Look up a public record",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )

    def request(self, **kwargs):
        max_output_tokens = kwargs.pop("max_output_tokens", 256)
        return GatewayRequestV1.create(
            trace_id="trace-066",
            project_id="local-default",
            model="router-model",
            messages=(self.message,),
            max_output_tokens=max_output_tokens,
            idempotency_key="idem-066",
            **kwargs,
        )

    def test_policy_is_contract_only_and_default_off(self):
        policy = public_gateway_policy()
        self.assertEqual(GATEWAY_SCHEMA_VERSION, policy["schema_version"])
        self.assertFalse(policy["network_enabled_by_default"])
        self.assertFalse(policy["provider_calls_enabled_by_default"])
        self.assertFalse(policy["paid_calls_enabled_by_default"])
        self.assertFalse(policy["provider_specific_fields_in_core"])

    def test_capabilities_round_trip(self):
        capabilities = GatewayCapabilitiesV1(
            backend_id="mock-backend",
            protocol="openai-compatible",
            deployment="mock",
            modalities=("text", "image"),
            supports_streaming=True,
            supports_cancellation=True,
            supports_structured_output=True,
            supports_tools=True,
            reports_usage=True,
            max_context_tokens=8192,
            max_output_tokens=1024,
        )
        self.assertEqual(capabilities.to_dict(), GatewayCapabilitiesV1.from_dict(capabilities.to_dict()).to_dict())

    def test_request_round_trip_preserves_structured_options(self):
        request = self.request(
            tools=(self.tool,),
            response_format={"type": "json_schema", "name": "summary"},
            temperature=0.2,
            stream=True,
            metadata={"task_family": "research", "risk": "low"},
        )
        restored = GatewayRequestV1.from_dict(request.to_dict())
        self.assertEqual(request.to_dict(), restored.to_dict())
        self.assertTrue(request.request_id.startswith("gateway_request_"))

    def test_request_id_changes_when_execution_options_change(self):
        base = self.request()
        changed = self.request(max_output_tokens=512)
        self.assertNotEqual(base.request_id, changed.request_id)

    def test_request_rejects_secret_metadata_and_duplicate_tools(self):
        with self.assertRaises(GatewayBackendContractError):
            self.request(metadata={"api_key": "redacted"})
        with self.assertRaises(GatewayBackendContractError):
            GatewayRequestV1(
                request_id="req-066",
                trace_id="trace-066",
                project_id="local-default",
                model="router-model",
                messages=(self.message,),
                max_output_tokens=32,
                idempotency_key="idem-066",
                tools=(self.tool, self.tool),
            )

    def test_request_rejects_unknown_serialized_fields(self):
        payload = self.request().to_dict()
        payload["provider_api_key"] = "must-not-cross-boundary"
        with self.assertRaises((GatewayBackendContractError, ValueError)):
            GatewayRequestV1.from_dict(payload)

    def test_usage_round_trip_and_invariants(self):
        usage = GatewayUsageV1(input_tokens=40, output_tokens=12, cached_input_tokens=10, reasoning_tokens=4)
        self.assertEqual(52, usage.total_tokens)
        self.assertEqual(usage.to_dict(), GatewayUsageV1.from_dict(usage.to_dict()).to_dict())
        with self.assertRaises(GatewayBackendContractError):
            GatewayUsageV1(input_tokens=2, cached_input_tokens=3)
        payload = usage.to_dict()
        payload["total_tokens"] = 999
        with self.assertRaises(GatewayBackendContractError):
            GatewayUsageV1.from_dict(payload)

    def test_billing_round_trip_and_missing_is_zero_only(self):
        missing = GatewayBillingV1()
        self.assertEqual(missing.to_dict(), GatewayBillingV1.from_dict(missing.to_dict()).to_dict())
        provider = GatewayBillingV1(
            source="provider",
            input_cost_usd=0.01,
            output_cost_usd=0.02,
            cached_input_cost_usd=0.001,
            total_cost_usd=0.031,
            reconciled=True,
        )
        self.assertEqual(provider.to_dict(), GatewayBillingV1.from_dict(provider.to_dict()).to_dict())
        with self.assertRaises(GatewayBackendContractError):
            GatewayBillingV1(input_cost_usd=0.01, total_cost_usd=0.01)
        with self.assertRaises(GatewayBackendContractError):
            GatewayBillingV1(source="provider", input_cost_usd=0.02, total_cost_usd=0.01)

    def test_stream_event_round_trip_and_response_order(self):
        request = self.request(stream=True)
        events = (
            GatewayStreamEventV1(request_id=request.request_id, sequence=0, event_type="started", payload={}),
            GatewayStreamEventV1(request_id=request.request_id, sequence=1, event_type="delta", payload={"text": "ok"}),
            GatewayStreamEventV1(request_id=request.request_id, sequence=2, event_type="completed", payload={}),
        )
        self.assertEqual(events[1].to_dict(), GatewayStreamEventV1.from_dict(events[1].to_dict()).to_dict())
        response = GatewayResponseV1(
            request_id=request.request_id,
            status="completed",
            content="ok",
            usage=GatewayUsageV1(input_tokens=3, output_tokens=1),
            billing=GatewayBillingV1(source="estimated", total_cost_usd=0.001),
            events=events,
        )
        self.assertEqual(response.to_dict(), GatewayResponseV1.from_dict(response.to_dict()).to_dict())
        self.assertIs(validate_response_for_request(request, response), response)
        with self.assertRaises(GatewayBackendContractError):
            GatewayResponseV1(request_id=request.request_id, status="completed", events=(events[1], events[0]))

    def test_failed_and_cancelled_responses_require_error(self):
        with self.assertRaises(GatewayBackendContractError):
            GatewayResponseV1(request_id="req-066", status="failed")
        error = GatewayErrorV1(category="timeout", message="backend timed out", retryable=True)
        response = GatewayResponseV1(request_id="req-066", status="failed", error=error)
        self.assertEqual(response.to_dict(), GatewayResponseV1.from_dict(response.to_dict()).to_dict())
        with self.assertRaises(GatewayBackendContractError):
            GatewayErrorV1(category="provider", message="Bearer top-secret-token", retryable=False)

    def test_streaming_and_non_streaming_response_requirements(self):
        stream_request = self.request(stream=True)
        with self.assertRaises(GatewayBackendContractError):
            validate_response_for_request(
                stream_request,
                GatewayResponseV1(request_id=stream_request.request_id, status="completed"),
            )
        non_stream_request = self.request()
        event = GatewayStreamEventV1(request_id=non_stream_request.request_id, sequence=0, event_type="started", payload={})
        with self.assertRaises(GatewayBackendContractError):
            validate_response_for_request(
                non_stream_request,
                GatewayResponseV1(request_id=non_stream_request.request_id, status="completed", events=(event,)),
            )
        with self.assertRaises(GatewayBackendContractError):
            validate_response_for_request(
                non_stream_request,
                GatewayResponseV1(request_id="other-request", status="completed"),
            )

    def test_cancellation_round_trip(self):
        cancellation = GatewayCancellationV1(request_id="req-066", accepted=True, reason="user requested")
        self.assertEqual(cancellation.to_dict(), GatewayCancellationV1.from_dict(cancellation.to_dict()).to_dict())

    def test_fake_backend_has_contract_shape_without_network(self):
        request = self.request()

        class FakeBackend:
            capabilities = GatewayCapabilitiesV1(
                backend_id="fake", protocol="custom", deployment="mock"
            )

            def submit(self, request):
                return GatewayResponseV1(request_id=request.request_id, status="completed", content="ok")

            def stream(self, request):
                yield GatewayStreamEventV1(
                    request_id=request.request_id, sequence=0, event_type="completed", payload={}
                )

            def cancel(self, request_id, *, reason):
                return GatewayCancellationV1(request_id=request_id, accepted=True, reason=reason)

        backend = FakeBackend()
        self.assertIsInstance(backend, GatewayBackend)
        self.assertEqual("completed", backend.submit(request).status)
        self.assertEqual(request.request_id, tuple(backend.stream(request))[0].request_id)
        self.assertTrue(backend.cancel(request.request_id, reason="done").accepted)


if __name__ == "__main__":
    unittest.main()
