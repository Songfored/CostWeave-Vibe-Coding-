from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from costweave.adapters import (
    DeepSeekChatHTTPAdapter,
    ExperimentalAdapterConfig,
    ExternalExecutionRequest,
)
from costweave.event_store import EventStore
from costweave.experimental_calls import ExperimentalCallStore
from costweave.planning_contracts import WorkOrder


class DeepSeekChatAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        database = Path(self.temporary.name) / "runtime.sqlite3"
        EventStore(database)
        self.store = ExperimentalCallStore(database)

    def tearDown(self):
        self.temporary.cleanup()

    @staticmethod
    def request() -> ExternalExecutionRequest:
        order = WorkOrder(
            work_order_id="work_order_deepseek_fixture",
            task_id="task_deepseek_fixture",
            objective="Return the synthetic answer and its evidence.",
            dependencies=(),
            input_artifacts=(),
            context_manifest=(),
            assumptions=("fixture only",),
            allowed_tools=(),
            forbidden_actions=("do not access external systems",),
            output_schema={
                "type": "object",
                "required": ["answer", "evidence"],
            },
            acceptance_tests=("answer and evidence are present",),
            cost_limit=0.10,
            time_limit_ms=30_000,
            escalation_conditions=("insufficient evidence",),
            idempotency_key="deepseek-work-order-fixture",
        )
        return ExternalExecutionRequest(
            experiment_id="experiment_deepseek_fixture",
            run_id="run_deepseek_fixture",
            task_id=order.task_id,
            work_order=order,
            max_output_tokens=512,
            idempotency_key="deepseek-call-fixture",
        )

    def adapter(self, effort: str) -> DeepSeekChatHTTPAdapter:
        return DeepSeekChatHTTPAdapter(
            ExperimentalAdapterConfig(
                adapter_id="deepseek-fixture",
                provider="deepseek-official",
                endpoint="https://api.deepseek.com/chat/completions",
                model_id="deepseek-v4-pro",
                deployment="remote",
                reasoning_effort=effort,
            ),
            store=self.store,
        )

    def test_low_effort_uses_non_thinking_json_mode(self):
        payload = self.adapter("low").build_payload(self.request())

        self.assertEqual({"type": "json_object"}, payload["response_format"])
        self.assertEqual({"type": "disabled"}, payload["thinking"])
        self.assertNotIn("reasoning_effort", payload)
        self.assertIn("JSON", payload["messages"][0]["content"])
        contract = json.loads(payload["messages"][1]["content"])
        self.assertEqual(
            {"answer": None, "evidence": None},
            contract["json_output_example"],
        )

    def test_high_effort_keeps_thinking_with_supported_effort(self):
        payload = self.adapter("medium").build_payload(self.request())

        self.assertEqual({"type": "enabled"}, payload["thinking"])
        self.assertEqual("high", payload["reasoning_effort"])
        self.assertEqual(512, payload["max_tokens"])

    def test_reasoning_only_response_is_diagnosed_without_persisting_cot(self):
        parsed = self.adapter("high").parse_response(
            {
                "id": "deepseek-response-fixture",
                "model": "deepseek-v4-pro",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {
                            "content": None,
                            "reasoning_content": "private chain of thought",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 120,
                    "completion_tokens": 512,
                    "prompt_cache_hit_tokens": 32,
                    "completion_tokens_details": {"reasoning_tokens": 512},
                },
            }
        )

        self.assertEqual("", parsed.content)
        self.assertTrue(parsed.reasoning_content_present)
        self.assertEqual(512, parsed.reasoning_tokens)
        self.assertEqual(32, parsed.cached_input_tokens)
        self.assertEqual("length", parsed.finish_reason)
        self.assertNotIn("private chain of thought", repr(parsed))


if __name__ == "__main__":
    unittest.main()
