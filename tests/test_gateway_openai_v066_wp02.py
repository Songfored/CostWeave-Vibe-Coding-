import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from costweave.adapters import (
    FakeOpenAICompatibleServer,
    GatewayBackend,
    GatewayHTTPResponse,
    GatewayMessageV1,
    GatewayRequestV1,
    GatewayToolV1,
    OpenAICompatibleGatewayBackend,
    OpenAICompatibleGatewayConfig,
    OpenAICompatibleGatewayError,
    OpenAICompatibleRuntimePolicy,
    GatewayBackendContractError,
    GatewayBackendDisabledError,
    public_openai_gateway_policy,
)


class StaticTransport:
    evidence_level = "E0-static-transport"

    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, *, url, headers, body, timeout_seconds, max_response_bytes):
        self.calls.append({
            "url": url,
            "headers": dict(headers),
            "body": bytes(body),
            "timeout_seconds": timeout_seconds,
            "max_response_bytes": max_response_bytes,
        })
        return self.response


def openai_response(*, content="fixture answer", status=200, usage=None, headers=None):
    value = {
        "id": "chatcmpl-fixture-001",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
    }
    if usage is not None:
        value["usage"] = usage
    return GatewayHTTPResponse(
        status_code=status,
        body=json.dumps(value, ensure_ascii=False).encode("utf-8"),
        headers=headers or {"Content-Type": "application/json"},
        elapsed_ms=4,
    )


class OpenAICompatibleGatewayTests(unittest.TestCase):
    def setUp(self):
        self.config = OpenAICompatibleGatewayConfig(
            backend_id="local-openai",
            endpoint="http://127.0.0.1:11434/v1/chat/completions",
        )
        self.tool = GatewayToolV1(
            name="lookup",
            description="Lookup a public record",
            input_schema={"type": "object", "properties": {"q": {"type": "string"}}},
        )

    def request(self, *, stream=False, structured=False):
        return GatewayRequestV1.create(
            trace_id="trace-wp02",
            project_id="local-default",
            model="local-model",
            messages=(
                GatewayMessageV1(role="system", content="Answer briefly"),
                GatewayMessageV1(role="user", content="hello", name="requester"),
            ),
            max_output_tokens=128,
            idempotency_key="idem-wp02",
            tools=(self.tool,),
            response_format={"type": "json_object"} if structured else None,
            temperature=0.2,
            stream=stream,
            metadata={"task_family": "classification"},
        )

    def backend(self, transport, *, enabled=True):
        return OpenAICompatibleGatewayBackend(
            self.config,
            policy=OpenAICompatibleRuntimePolicy(network_enabled=enabled),
            transport=transport,
        )

    def test_default_policy_is_disabled_and_does_not_call_transport(self):
        fake = FakeOpenAICompatibleServer()
        backend = OpenAICompatibleGatewayBackend(self.config, transport=fake)
        with self.assertRaises(GatewayBackendDisabledError):
            backend.submit(self.request())
        self.assertEqual([], fake.requests)
        self.assertFalse(public_openai_gateway_policy()["network_enabled"])

    def test_remote_endpoint_and_unsafe_url_are_rejected(self):
        with self.assertRaises(OpenAICompatibleGatewayError):
            OpenAICompatibleGatewayConfig(
                backend_id="remote",
                endpoint="https://api.example.com/v1/chat/completions",
            )
        with self.assertRaises(OpenAICompatibleGatewayError):
            OpenAICompatibleGatewayConfig(
                backend_id="query",
                endpoint="http://127.0.0.1:8000/v1/chat/completions?token=secret",
            )
        with self.assertRaises(OpenAICompatibleGatewayError):
            OpenAICompatibleGatewayConfig(
                backend_id="credential",
                endpoint="http://user:pass@127.0.0.1:8000/v1/chat/completions",
            )

    def test_fake_server_submit_maps_wire_contract_without_schema_leak(self):
        fake = FakeOpenAICompatibleServer(
            content="fake answer",
            input_tokens=20,
            output_tokens=7,
            cached_input_tokens=3,
        )
        backend = self.backend(fake)
        response = backend.submit(self.request())
        self.assertIsInstance(backend, GatewayBackend)
        self.assertEqual("completed", response.status)
        self.assertEqual("fake answer", response.content)
        self.assertEqual("fake-request-001", response.provider_request_id)
        self.assertEqual(20, response.usage.input_tokens)
        self.assertEqual(7, response.usage.output_tokens)
        self.assertEqual(3, response.usage.cached_input_tokens)
        self.assertEqual("missing", response.billing.source)
        self.assertEqual(1, len(fake.requests))
        payload = fake.requests[0]["payload"]
        self.assertEqual("local-model", payload["model"])
        self.assertNotIn("schema_version", payload["messages"][0])
        self.assertEqual("requester", payload["messages"][1]["name"])
        self.assertEqual("lookup", payload["tools"][0]["function"]["name"])
        self.assertNotIn("Authorization", fake.requests[0]["headers"])

    def test_structured_output_is_parsed_only_when_requested(self):
        fake = FakeOpenAICompatibleServer(content='{"answer":"ok"}')
        response = self.backend(fake).submit(self.request(structured=True))
        self.assertEqual({"answer": "ok"}, response.structured_output)
        plain = self.backend(FakeOpenAICompatibleServer(content='{"answer":"ok"}')).submit(self.request())
        self.assertIsNone(plain.structured_output)

    def test_provider_rate_limit_maps_to_retryable_error(self):
        fake = FakeOpenAICompatibleServer(status_code=429)
        response = self.backend(fake).submit(self.request())
        self.assertEqual("failed", response.status)
        self.assertEqual("rate_limit", response.error.category)
        self.assertTrue(response.error.retryable)

    def test_invalid_json_fails_closed_as_protocol_error(self):
        transport = StaticTransport(
            GatewayHTTPResponse(200, b"not-json", {"X-Request-ID": "wire-id"}, 2)
        )
        response = self.backend(transport).submit(self.request())
        self.assertEqual("failed", response.status)
        self.assertEqual("protocol", response.error.category)
        self.assertFalse(response.tool_calls)

    def test_invalid_usage_fails_closed_as_protocol_error(self):
        response = openai_response(
            usage={
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "prompt_tokens_details": {"cached_tokens": 3},
            }
        )
        result = self.backend(StaticTransport(response)).submit(self.request())
        self.assertEqual("failed", result.status)
        self.assertEqual("protocol", result.error.category)

    def test_provider_billing_header_is_structured_and_not_required(self):
        response = openai_response(
            usage={"prompt_tokens": 3, "completion_tokens": 2},
            headers={"X-Cost-USD": "0.004"},
        )
        result = self.backend(StaticTransport(response)).submit(self.request())
        self.assertEqual("provider", result.billing.source)
        self.assertAlmostEqual(0.004, result.billing.total_cost_usd)
        self.assertTrue(result.billing.reconciled)

    def test_stream_maps_delta_usage_and_done_events(self):
        fake = FakeOpenAICompatibleServer(content="streamed")
        request = self.request(stream=True)
        events = tuple(self.backend(fake).stream(request))
        self.assertEqual(
            ["started", "delta", "usage", "completed"],
            [event.event_type for event in events],
        )
        self.assertEqual(list(range(len(events))), [event.sequence for event in events])
        self.assertEqual("streamed", events[1].payload["text"])
        self.assertEqual("fake-request-001", events[-1].provider_request_id)

    def test_non_stream_and_stream_entrypoints_are_not_mixed(self):
        fake = FakeOpenAICompatibleServer()
        with self.assertRaises(OpenAICompatibleGatewayError):
            self.backend(fake).submit(self.request(stream=True))
        with self.assertRaises(OpenAICompatibleGatewayError):
            tuple(self.backend(fake).stream(self.request(stream=False)))
        self.assertEqual([], fake.requests)

    def test_non_success_stream_response_emits_error_event(self):
        response = openai_response(status=503)
        events = tuple(self.backend(StaticTransport(response)).stream(self.request(stream=True)))
        self.assertEqual(["started", "error"], [event.event_type for event in events])
        self.assertEqual("overloaded", events[1].payload["category"])
        self.assertTrue(events[1].payload["retryable"])

    def test_cancel_is_explicitly_unsupported_and_never_claims_acceptance(self):
        fake = FakeOpenAICompatibleServer()
        backend = self.backend(fake)
        cancellation = backend.cancel("request-to-cancel", reason="user requested")
        self.assertFalse(cancellation.accepted)
        self.assertFalse(backend.capabilities.supports_cancellation)
        self.assertEqual([], fake.requests)

    def test_cancelled_stream_stops_before_provider_completion(self):
        fake = FakeOpenAICompatibleServer(content="would be streamed")
        backend = self.backend(fake)
        request = self.request(stream=True)
        backend.cancel(request.request_id, reason="stop")
        events = tuple(backend.stream(request))
        self.assertEqual("cancelled", events[-1].event_type)
        self.assertEqual([], fake.requests)

    def test_explicit_loopback_endpoint_works_with_real_local_transport(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                self.rfile.read(length)
                body = json.dumps({
                    "id": "loopback-request-001",
                    "choices": [{"message": {"role": "assistant", "content": "loopback ok"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1},
                }).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            config = OpenAICompatibleGatewayConfig(
                backend_id="explicit-loopback",
                endpoint=f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
            )
            backend = OpenAICompatibleGatewayBackend(
                config,
                policy=OpenAICompatibleRuntimePolicy(network_enabled=True),
            )
            response = backend.submit(self.request())
            self.assertEqual("completed", response.status)
            self.assertEqual("loopback ok", response.content)
            self.assertEqual("loopback-request-001", response.provider_request_id)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_config_and_response_boundaries_are_strict(self):
        with self.assertRaises(OpenAICompatibleGatewayError):
            OpenAICompatibleGatewayConfig(
                backend_id="bad",
                endpoint="http://127.0.0.1:8000/v1/chat/completions",
                max_response_bytes=4_000_001,
            )
        with self.assertRaises(GatewayBackendContractError):
            GatewayHTTPResponse(99, b"", {}, 0)


if __name__ == "__main__":
    unittest.main()
