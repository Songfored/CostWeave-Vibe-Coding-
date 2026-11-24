from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from costweave.kernel.contracts import Job, Operation
from costweave.kernel.http import dispatch as http_dispatch
from costweave.kernel.ports import CancellationToken, default_registry
from costweave.kernel.provider_port import register_streaming_model_route
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore
from costweave.kernel.transports import (
    ModelEndpointConfig,
    OpenAICompatibleTransport,
    StreamResponse,
    TokenUsage,
    TransportError,
    compute_micro_usd,
)


class _FakeSSETransport:
    def __init__(self, lines: list[bytes], *, headers: dict[str, str] | None = None, status: int = 200) -> None:
        self.lines = lines
        self.headers = headers or {"content-type": "text/event-stream"}
        self.status = status
        self.closed = False
        self.calls: list[dict] = []

    def stream_post(self, *, url, headers, body, timeout_seconds, cancel_token=None) -> StreamResponse:
        self.calls.append({"url": url, "headers": dict(headers), "body": json.loads(body)})

        def gen():
            for line in self.lines:
                if cancel_token is not None and cancel_token.cancelled:
                    self.closed = True
                    break
                yield line

        def close_fn() -> None:
            self.closed = True

        return StreamResponse(self.status, self.headers, gen(), close_fn=close_fn)


class KernelTransportTests(unittest.TestCase):
    def test_sse_stream_usage_cached_and_reasoning_micro_usd(self) -> None:
        cfg = ModelEndpointConfig(
            name="gpt-stream",
            provider_family="openai",
            endpoint="https://api.example.invalid/v1/chat/completions",
            model_id="gpt-5.6-luna",
            deployment="remote",
            input_price_per_mtok_usd=2.0,
            cached_input_price_per_mtok_usd=0.2,
            output_price_per_mtok_usd=10.0,
            reasoning_price_per_mtok_usd=10.0,
        )
        # 1000 prompt (800 cached, 200 uncached): 200*2 + 800*0.2 = 400 + 160 = 560 micro-USD
        # 500 completion (300 reasoning, 200 normal): 500*10 = 5000 micro-USD
        # Total = 5560 micro-USD
        lines = [
            b'data: {"choices":[{"delta":{"content":"{\\"ans\\":"}}]}\n\n',
            b'data: {"choices":[{"delta":{"content":"42}"}}]}\n\n',
            b'data: {"choices":[],"usage":{"prompt_tokens":1000,"completion_tokens":500,"prompt_tokens_details":{"cached_tokens":800},"completion_tokens_details":{"reasoning_tokens":300}}}\n\n',
            b"data: [DONE]\n\n",
        ]
        transport = _FakeSSETransport(lines)
        reg = default_registry()
        register_streaming_model_route(reg, cfg, transport=transport, live_enabled=True)

        job = Job(
            ["answer"], {"q": "6*7"}, 10_000,
            [Operation("solve", ["q"], "answer", "gpt-stream", "present", 8_000, {"prompt": "Solve"})],
            acceptance={"answer": {"validator": "present", "config": {}}},
        )
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        state = Runtime(store, reg).submit("sse-ok", job)
        self.assertEqual("complete", state["status"])
        self.assertEqual({"ans": 42}, state["artifacts"]["answer"])
        self.assertEqual(5560, state["spent_units"])

    def test_mid_stream_cancellation_aborts_immediately_and_settles_partial(self) -> None:
        cfg = ModelEndpointConfig(
            name="stream-cancel",
            provider_family="deepseek",
            endpoint="http://127.0.0.1:8000/v1/chat/completions",
            model_id="ds-local",
            deployment="local",
            input_price_per_mtok_usd=1.0,
            output_price_per_mtok_usd=2.0,
        )
        tok = CancellationToken()

        def chunk_iter():
            yield b'data: {"choices":[{"delta":{"content":"{\\"part\\":"}}]}\n\n'
            yield b'data: {"choices":[{"delta":{"content":"1"}}]}\n\n'
            tok.cancel("sibling_failed")
            yield b'data: {"choices":[{"delta":{"content":",\\"never\\":true}"}}]}\n\n'

        class _CancelTransport:
            def __init__(self):
                self.closed = False

            def stream_post(self, *, url, headers, body, timeout_seconds, cancel_token=None):
                return StreamResponse(200, {"content-type": "text/event-stream"}, chunk_iter(),
                                      close_fn=lambda: setattr(self, "closed", True))

        ct = _CancelTransport()
        client = OpenAICompatibleTransport(cfg, transport=ct)
        receipt = client.invoke(prompt="test", inputs_payload={"x": 1}, idempotency_key="k1", cancel_token=tok)
        self.assertTrue(receipt.aborted)
        self.assertTrue(ct.closed)
        self.assertEqual("aborted_partial", receipt.billing_source)
        self.assertGreater(receipt.actual_units or 0, 0)
        self.assertEqual(2, receipt.chunks_received)

    def test_secret_echo_in_sse_chunk_is_blocked(self) -> None:
        os.environ["COSTWEAVE_TEST_API_SECRET"] = "sk-live-secret-token-987654"
        try:
            cfg = ModelEndpointConfig(
                name="secret-leak",
                provider_family="openai",
                endpoint="https://api.example.invalid/v1/chat/completions",
                model_id="m1",
                deployment="remote",
                secret_env_var="COSTWEAVE_TEST_API_SECRET",
            )
            lines = [
                b'data: {"choices":[{"delta":{"content":"sk-live-secret-token-987654"}}]}\n\n',
            ]
            transport = _FakeSSETransport(lines)
            reg = default_registry()
            register_streaming_model_route(reg, cfg, transport=transport, live_enabled=True)
            job = Job(
                ["answer"], {}, 100,
                [Operation("call", [], "answer", "secret-leak", "present", 10, {"prompt": "hi"})],
                acceptance={"answer": {"validator": "present", "config": {}}},
            )
            store = KernelStore(":memory:")
            self.addCleanup(store.close)
            state = Runtime(store, reg).submit("leak-run", job)
            self.assertEqual("blocked", state["status"])
            self.assertEqual("unreachable", state["reason"])
            self.assertNotIn("answer", state["artifacts"])
            self.assertNotIn("sk-live-secret-token-987654", json.dumps(store.events("leak-run")))
        finally:
            os.environ.pop("COSTWEAVE_TEST_API_SECRET", None)

    def test_live_disabled_by_default_stops_with_zero_spend(self) -> None:
        cfg = ModelEndpointConfig(
            name="disabled-ep",
            provider_family="openai",
            endpoint="https://api.example.invalid/v1/chat/completions",
            model_id="m1",
            deployment="remote",
        )
        reg = default_registry()
        register_streaming_model_route(reg, cfg, live_enabled=False)
        job = Job(
            ["answer"], {}, 100,
            [Operation("call", [], "answer", "disabled-ep", "present", 50, {"prompt": "hi"})],
            acceptance={"answer": {"validator": "present", "config": {}}},
        )
        store = KernelStore(":memory:")
        self.addCleanup(store.close)
        state = Runtime(store, reg).submit("disabled-run", job)
        self.assertEqual("blocked", state["status"])
        self.assertEqual("admission_denied", state["reason"])
        self.assertEqual(0, state["spent_units"])

    def test_in_process_v2_http_dispatch_audit_and_reconcile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "v1.sqlite3"
            from costweave.control_kernel import ControlKernel
            kernel = ControlKernel(str(db_path))
            job_body = {
                "goals": ["out"],
                "inputs": {"src": {"val": 7}},
                "budget_units": 10,
                "planning_policy": "bsao",
                "operations": [
                    {"id": "p", "requires": ["src"], "produces": "out", "executor": "pick",
                     "validator": "present", "cost_units": 2, "config": {"source": "src", "path": ["val"]}}
                ],
                "acceptance": {"out": {"validator": "equals", "config": {"expected": 7}}},
            }
            st, res = http_dispatch("POST", "/api/v2/runs", job_body, "v2-audit-1", kernel, "proj-a")
            self.assertEqual(200, int(st))
            self.assertEqual("complete", res["status"])
            st_a, audit = http_dispatch("GET", "/api/v2/runs/v2-audit-1/audit", None, None, kernel, "proj-a")
            self.assertEqual(200, int(st_a))
            self.assertEqual("bsao", audit["planning_policy"])
            self.assertEqual(1, len(audit["decisions"]))
            self.assertIn("audit", audit["decisions"][0]["plan"])


if __name__ == "__main__":
    unittest.main()
