import json
import traceback
import unittest
from datetime import datetime, timedelta, timezone

from costweave.adapters import (
    FakeOpenAICompatibleServer,
    GatewayBackendDisabledError,
    GatewayMessageV1,
    GatewayRequestV1,
    LocalCapacitySnapshot,
    LocalEndpointAdmissionError,
    LocalEndpointAdmissionPolicy,
    LocalEndpointConfig,
    LocalOpenAICompatibleGatewayBackend,
    OpenAICompatibleGatewayError,
    OpenAICompatibleRuntimePolicy,
    StaticLocalCapacityProbe,
    public_local_endpoint_policy,
)


class LocalEndpointGatewayTests(unittest.TestCase):
    def setUp(self):
        self.config = LocalEndpointConfig(
            backend_id="ollama-local",
            endpoint="http://127.0.0.1:11434/v1/chat/completions",
            endpoint_kind="ollama",
        )
        self.request = GatewayRequestV1.create(
            trace_id="trace-wp04",
            project_id="local-default",
            model="llama3.2",
            messages=(GatewayMessageV1(role="user", content="hello"),),
            max_output_tokens=32,
            idempotency_key="idem-wp04",
        )

    @staticmethod
    def snapshot(**overrides):
        values = {
            "snapshot_id": "capacity-001",
            "source": "fixture",
            "queue_depth": 0,
            "queue_limit": 8,
            "available_slots": 2,
            "estimated_wait_ms": 0,
            "telemetry_age_ms": 10,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }
        values.update(overrides)
        return LocalCapacitySnapshot(**values)

    def backend(self, *, snapshot=None, network=True, capacity=True, transport=None, **policy):
        return LocalOpenAICompatibleGatewayBackend(
            self.config,
            policy=OpenAICompatibleRuntimePolicy(network_enabled=network),
            capacity_policy=LocalEndpointAdmissionPolicy(
                capacity_admission_enabled=capacity,
                **policy,
            ),
            capacity_probe=StaticLocalCapacityProbe(snapshot),
            transport=transport or FakeOpenAICompatibleServer(content="local ok"),
        )

    def test_public_policy_is_default_off_and_fail_closed(self):
        policy = public_local_endpoint_policy()
        self.assertFalse(policy["network_enabled"])
        self.assertFalse(policy["capacity_admission_enabled"])
        self.assertTrue(policy["missing_telemetry_fails_closed"])

    def test_default_policy_blocks_before_transport(self):
        fake = FakeOpenAICompatibleServer()
        backend = LocalOpenAICompatibleGatewayBackend(
            self.config,
            transport=fake,
            capacity_probe=StaticLocalCapacityProbe(self.snapshot()),
        )
        with self.assertRaises(LocalEndpointAdmissionError) as context:
            backend.submit(self.request)
        self.assertEqual("network_disabled", context.exception.decision.reason_code)
        self.assertEqual([], fake.requests)

    def test_capacity_policy_must_be_explicitly_enabled(self):
        fake = FakeOpenAICompatibleServer()
        backend = self.backend(transport=fake, capacity=False)
        with self.assertRaises(LocalEndpointAdmissionError) as context:
            backend.submit(self.request)
        self.assertEqual("capacity_admission_disabled", context.exception.decision.reason_code)
        self.assertEqual([], fake.requests)

    def test_missing_probe_fails_closed_before_transport(self):
        fake = FakeOpenAICompatibleServer()
        backend = LocalOpenAICompatibleGatewayBackend(
            self.config,
            policy=OpenAICompatibleRuntimePolicy(network_enabled=True),
            capacity_policy=LocalEndpointAdmissionPolicy(capacity_admission_enabled=True),
            transport=fake,
        )
        with self.assertRaises(LocalEndpointAdmissionError) as context:
            backend.submit(self.request)
        self.assertEqual("telemetry_missing", context.exception.decision.reason_code)
        self.assertEqual([], fake.requests)

    def test_incomplete_and_stale_telemetry_fail_closed(self):
        for snapshot, expected in (
            (self.snapshot(estimated_wait_ms=None), "telemetry_incomplete"),
            (self.snapshot(telemetry_age_ms=30_001), "telemetry_stale"),
        ):
            fake = FakeOpenAICompatibleServer()
            backend = self.backend(snapshot=snapshot, transport=fake)
            with self.assertRaises(LocalEndpointAdmissionError) as context:
                backend.submit(self.request)
            self.assertEqual(expected, context.exception.decision.reason_code)
            self.assertEqual([], fake.requests)

    def test_captured_at_age_cannot_be_underreported(self):
        fake = FakeOpenAICompatibleServer()
        snapshot = self.snapshot(
            captured_at=(datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            telemetry_age_ms=0,
        )
        backend = self.backend(snapshot=snapshot, transport=fake)
        with self.assertRaises(LocalEndpointAdmissionError) as context:
            backend.submit(self.request)
        self.assertEqual("telemetry_stale", context.exception.decision.reason_code)
        self.assertEqual([], fake.requests)

    def test_captured_at_too_far_in_future_fails_closed(self):
        fake = FakeOpenAICompatibleServer()
        snapshot = self.snapshot(
            captured_at=(datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat(),
            telemetry_age_ms=0,
        )
        backend = self.backend(snapshot=snapshot, transport=fake)
        with self.assertRaises(LocalEndpointAdmissionError) as context:
            backend.submit(self.request)
        self.assertEqual("telemetry_from_future", context.exception.decision.reason_code)
        self.assertEqual([], fake.requests)

    def test_complete_telemetry_requirement_cannot_be_disabled(self):
        with self.assertRaises(OpenAICompatibleGatewayError):
            LocalEndpointAdmissionPolicy(
                capacity_admission_enabled=True,
                require_complete_telemetry=False,
            )

    def test_queue_and_slot_limits_are_hard_constraints(self):
        for snapshot, expected in (
            (self.snapshot(available_slots=0), "insufficient_slots"),
            (self.snapshot(queue_depth=1), "queue_depth_exceeded"),
            (self.snapshot(estimated_wait_ms=1), "estimated_wait_exceeded"),
        ):
            fake = FakeOpenAICompatibleServer()
            backend = self.backend(snapshot=snapshot, transport=fake)
            with self.assertRaises(LocalEndpointAdmissionError) as context:
                backend.submit(self.request)
            self.assertEqual(expected, context.exception.decision.reason_code)
            self.assertEqual([], fake.requests)

    def test_admitted_snapshot_calls_openai_contract_and_records_decision(self):
        fake = FakeOpenAICompatibleServer(content="admitted")
        probe = StaticLocalCapacityProbe(self.snapshot())
        backend = LocalOpenAICompatibleGatewayBackend(
            self.config,
            policy=OpenAICompatibleRuntimePolicy(network_enabled=True),
            capacity_policy=LocalEndpointAdmissionPolicy(capacity_admission_enabled=True),
            capacity_probe=probe,
            transport=fake,
        )
        response = backend.submit(self.request)
        self.assertEqual("completed", response.status)
        self.assertEqual("admitted", backend.last_admission.reason_code)
        self.assertTrue(backend.last_admission.admitted)
        self.assertEqual(1, len(probe.calls))
        self.assertEqual(1, len(fake.requests))
        self.assertEqual("llama3.2", fake.requests[0]["payload"]["model"])

    def test_probe_exception_fails_closed_and_does_not_leak_error(self):
        class BrokenProbe:
            def read(self, *, backend_id, model):
                raise RuntimeError("secret probe details")

        fake = FakeOpenAICompatibleServer()
        backend = LocalOpenAICompatibleGatewayBackend(
            self.config,
            policy=OpenAICompatibleRuntimePolicy(network_enabled=True),
            capacity_policy=LocalEndpointAdmissionPolicy(capacity_admission_enabled=True),
            capacity_probe=BrokenProbe(),
            transport=fake,
        )
        with self.assertRaises(LocalEndpointAdmissionError) as context:
            backend.submit(self.request)
        self.assertEqual("telemetry_probe_failed", context.exception.decision.reason_code)
        self.assertNotIn("secret probe details", str(context.exception))
        self.assertIsNone(context.exception.__cause__)
        self.assertIsNone(context.exception.__context__)
        self.assertNotIn(
            "secret probe details",
            "".join(traceback.format_exception(context.exception)),
        )
        self.assertEqual([], fake.requests)

    def test_invalid_probe_value_fails_closed_before_transport(self):
        class InvalidProbe:
            def read(self, *, backend_id, model):
                return {"available_slots": 4}

        fake = FakeOpenAICompatibleServer()
        backend = LocalOpenAICompatibleGatewayBackend(
            self.config,
            policy=OpenAICompatibleRuntimePolicy(network_enabled=True),
            capacity_policy=LocalEndpointAdmissionPolicy(capacity_admission_enabled=True),
            capacity_probe=InvalidProbe(),
            transport=fake,
        )
        with self.assertRaises(LocalEndpointAdmissionError) as context:
            backend.submit(self.request)
        self.assertEqual("telemetry_invalid", context.exception.decision.reason_code)
        self.assertEqual([], fake.requests)

    def test_endpoint_kinds_are_explicit_and_remote_is_rejected(self):
        self.assertEqual("vllm", LocalEndpointConfig(
            backend_id="vllm-local",
            endpoint="http://localhost:8000/v1/chat/completions",
            endpoint_kind="vllm",
        ).endpoint_kind)
        with self.assertRaises(OpenAICompatibleGatewayError):
            LocalEndpointConfig(
                backend_id="remote",
                endpoint="https://provider.example/v1/chat/completions",
                endpoint_kind="vllm",
            )

    def test_snapshot_bounds_and_queue_order_are_strict(self):
        with self.assertRaises(OpenAICompatibleGatewayError):
            self.snapshot(queue_depth=9, queue_limit=8)
        with self.assertRaises(OpenAICompatibleGatewayError):
            self.snapshot(telemetry_age_ms=86_400_001)

    def test_stream_requires_capacity_admission_before_provider(self):
        fake = FakeOpenAICompatibleServer(content="streamed")
        backend = self.backend(snapshot=self.snapshot(), transport=fake)
        request = GatewayRequestV1.create(
            trace_id="trace-wp04-stream",
            project_id="local-default",
            model="llama3.2",
            messages=(GatewayMessageV1(role="user", content="stream"),),
            max_output_tokens=32,
            idempotency_key="idem-wp04-stream",
            stream=True,
        )
        events = tuple(backend.stream(request))
        self.assertEqual("started", events[0].event_type)
        self.assertEqual("completed", events[-1].event_type)
        self.assertEqual(1, len(fake.requests))

    def test_stream_admission_occurs_when_iteration_starts(self):
        fake = FakeOpenAICompatibleServer(content="must not be sent")
        probe = StaticLocalCapacityProbe(self.snapshot())
        backend = LocalOpenAICompatibleGatewayBackend(
            self.config,
            policy=OpenAICompatibleRuntimePolicy(network_enabled=True),
            capacity_policy=LocalEndpointAdmissionPolicy(capacity_admission_enabled=True),
            capacity_probe=probe,
            transport=fake,
        )
        request = GatewayRequestV1.create(
            trace_id="trace-wp04-delayed-stream",
            project_id="local-default",
            model="llama3.2",
            messages=(GatewayMessageV1(role="user", content="stream later"),),
            max_output_tokens=32,
            idempotency_key="idem-wp04-delayed-stream",
            stream=True,
        )
        events = backend.stream(request)
        probe.snapshot = self.snapshot(available_slots=0)
        with self.assertRaises(LocalEndpointAdmissionError) as context:
            tuple(events)
        self.assertEqual("insufficient_slots", context.exception.decision.reason_code)
        self.assertEqual(1, len(probe.calls))
        self.assertEqual([], fake.requests)

    def test_public_backend_redacts_no_secrets_and_exposes_evidence(self):
        backend = self.backend(snapshot=self.snapshot())
        public = backend.public()
        self.assertEqual("ollama", public["endpoint_kind"])
        self.assertTrue(public["capacity_probe_injected"])
        self.assertEqual("E0-static-capacity", public["evidence_level"])
        self.assertNotIn("Authorization", json.dumps(public))


if __name__ == "__main__":
    unittest.main()
