import json
import os
import tempfile
import unittest
from pathlib import Path

from costweave.adapters import (
    AdapterRuntimePolicy,
    ExperimentalAdapterConfig,
    ExternalExecutionRequest,
    HTTPResponse,
    LocalHTTPAdapter,
    OpenAICompatibleHTTPAdapter,
)
from costweave.domain import RunMode, TaskContract, WorkerProfile
from costweave.event_store import EventStore, RUNTIME_SCHEMA_VERSION
from costweave.experimental_calls import (
    ExperimentBudget,
    ExperimentalCallError,
    ExperimentalCallStore,
    ExternalBudgetExceededError,
    ExternalCallsDisabledError,
    ExternalIdempotencyError,
)
from costweave.planning_contracts import WorkOrder
from costweave.router import PredictiveRouter
from costweave.shadow_router import (
    ShadowIsolationError,
    ShadowRouter,
    ShadowStore,
)


class ScriptedTransport:
    evidence_level = "E0-transport-fixture"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, *, url, headers, body, timeout_seconds):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "body": bytes(body),
                "timeout_seconds": timeout_seconds,
            }
        )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def compatible_response(
    *, status=200, cost_header=None, content="fixture answer"
):
    headers = {"x-queue-time-ms": "4"}
    if cost_header is not None:
        headers["x-billed-usd"] = str(cost_header)
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 120,
            "completion_tokens": 30,
            "prompt_tokens_details": {"cached_tokens": 20},
        },
    }
    return HTTPResponse(
        status_code=status,
        body=json.dumps(body).encode(),
        headers=headers,
        elapsed_ms=12,
    )


def local_response():
    return HTTPResponse(
        status_code=200,
        body=json.dumps(
            {
                "output": "local fixture",
                "usage": {
                    "input_tokens": 40,
                    "output_tokens": 12,
                    "cached_input_tokens": 0,
                },
            }
        ).encode(),
        headers={},
        elapsed_ms=7,
    )


class ExperimentalAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        EventStore(self.path)
        self.store = ExperimentalCallStore(self.path)

    def tearDown(self):
        os.environ.pop("COSTWEAVE_TEST_SECRET", None)
        self.temp.cleanup()

    def budget(
        self,
        *,
        experiment_id="experiment_v0563",
        total=1.0,
        provider=1.0,
        max_calls=10,
        max_attempts=3,
        provider_name="fixture-provider",
    ):
        value = ExperimentBudget(
            experiment_id=experiment_id,
            total_cost_limit_usd=total,
            provider_cost_limits_usd={provider_name: provider},
            max_logical_calls=max_calls,
            max_attempts_per_call=max_attempts,
        )
        self.store.register_experiment(value)
        return value

    def work_order(self, *, cost_limit=1.0, tools=(), objective="answer"):
        return WorkOrder(
            work_order_id="work_order_v0563",
            task_id="task_v0563",
            objective=objective,
            dependencies=(),
            input_artifacts=(),
            context_manifest=(),
            assumptions=("fixture only",),
            allowed_tools=tuple(tools),
            forbidden_actions=("expand scope",),
            output_schema={"content": "string"},
            acceptance_tests=("contains an answer",),
            cost_limit=cost_limit,
            time_limit_ms=30000,
            escalation_conditions=("insufficient evidence",),
            idempotency_key="work-order-key",
        )

    def request(self, *, key="call-key", work_order=None, batch=False):
        return ExternalExecutionRequest(
            experiment_id="experiment_v0563",
            run_id="run_v0563",
            task_id="task_v0563",
            work_order=work_order or self.work_order(),
            max_output_tokens=100,
            idempotency_key=key,
            batch=batch,
        )

    def remote_config(self, **changes):
        values = {
            "adapter_id": "fixture-openai-compatible",
            "provider": "fixture-provider",
            "endpoint": "https://example.invalid/v1/chat/completions",
            "model_id": "fixture-model",
            "deployment": "remote",
            "input_price_per_mtok_usd": 1.0,
            "output_price_per_mtok_usd": 2.0,
            "max_retries": 0,
        }
        values.update(changes)
        return ExperimentalAdapterConfig(**values)

    @staticmethod
    def enabled_policy(*, paid=True):
        return AdapterRuntimePolicy(
            experimental_calls_enabled=True,
            paid_calls_enabled=paid,
        )

    def adapter(self, transport, *, config=None, policy=None):
        return OpenAICompatibleHTTPAdapter(
            config or self.remote_config(),
            store=self.store,
            policy=policy or self.enabled_policy(),
            transport=transport,
        )

    def test_default_off_rejects_before_transport(self):
        transport = ScriptedTransport([compatible_response()])
        adapter = self.adapter(
            transport, policy=AdapterRuntimePolicy()
        )
        with self.assertRaises(ExternalCallsDisabledError):
            adapter.execute(self.request())
        self.assertEqual([], transport.calls)
        self.assertEqual(0, self.store.counts()["external_calls"])

    def test_paid_calls_need_second_explicit_switch(self):
        transport = ScriptedTransport([compatible_response()])
        adapter = self.adapter(
            transport, policy=self.enabled_policy(paid=False)
        )
        with self.assertRaises(ExternalCallsDisabledError):
            adapter.execute(self.request())
        self.assertEqual([], transport.calls)

    def test_remote_requires_https_and_local_requires_loopback(self):
        with self.assertRaises(ExperimentalCallError):
            self.remote_config(endpoint="http://example.invalid/v1")
        with self.assertRaises(ExperimentalCallError):
            ExperimentalAdapterConfig(
                adapter_id="bad-local",
                provider="local",
                endpoint="http://192.168.1.3:8000/generate",
                model_id="local",
                deployment="local",
            )

    def test_secret_is_environment_only_and_required(self):
        config = self.remote_config(secret_env="COSTWEAVE_TEST_SECRET")
        transport = ScriptedTransport([compatible_response()])
        adapter = self.adapter(transport, config=config)
        with self.assertRaises(ExternalCallsDisabledError):
            adapter.execute(self.request())
        self.assertEqual([], transport.calls)

    def test_unregistered_budget_rejects_before_transport(self):
        transport = ScriptedTransport([compatible_response()])
        with self.assertRaises(ExternalBudgetExceededError):
            self.adapter(transport).execute(self.request())
        self.assertEqual([], transport.calls)

    def test_provider_budget_is_mandatory(self):
        self.budget(provider_name="different-provider")
        transport = ScriptedTransport([compatible_response()])
        with self.assertRaises(ExternalBudgetExceededError):
            self.adapter(transport).execute(self.request())
        self.assertEqual([], transport.calls)

    def test_work_order_reserves_all_retries_before_network(self):
        self.budget(max_attempts=3)
        transport = ScriptedTransport([compatible_response()])
        config = self.remote_config(max_retries=2)
        too_small = self.work_order(cost_limit=0.00001)
        with self.assertRaises(ExternalBudgetExceededError):
            self.adapter(transport, config=config).execute(
                self.request(work_order=too_small)
            )
        self.assertEqual([], transport.calls)

    def test_success_has_attribution_telemetry_and_pending_validation(self):
        self.budget()
        transport = ScriptedTransport([compatible_response()])
        receipt = self.adapter(transport).execute(self.request())
        self.assertEqual("completed", receipt.result.status)
        self.assertFalse(receipt.result.acceptance_results[0]["passed"])
        self.assertEqual(
            "E0-transport-fixture", receipt.telemetry["evidence_level"]
        )
        self.assertEqual("run_v0563", receipt.telemetry["run_id"])
        self.assertEqual("task_v0563", receipt.telemetry["task_id"])
        self.assertEqual(120, receipt.telemetry["input_tokens"])
        self.assertEqual(30, receipt.telemetry["output_tokens"])
        self.assertEqual(4, receipt.telemetry["queue_latency_ms"])
        self.assertFalse(receipt.telemetry["commercial_claims_allowed"])

    def test_idempotent_replay_does_not_call_transport_twice(self):
        self.budget()
        transport = ScriptedTransport([compatible_response()])
        adapter = self.adapter(transport)
        first = adapter.execute(self.request())
        replay = adapter.execute(self.request())
        self.assertEqual(first.external_call_id, replay.external_call_id)
        self.assertTrue(replay.replayed)
        self.assertEqual(1, len(transport.calls))

    def test_idempotency_key_reuse_for_new_request_is_rejected(self):
        self.budget()
        transport = ScriptedTransport(
            [compatible_response(), compatible_response()]
        )
        adapter = self.adapter(transport)
        adapter.execute(self.request())
        changed = self.work_order(objective="different objective")
        with self.assertRaises(ExternalIdempotencyError):
            adapter.execute(self.request(work_order=changed))
        self.assertEqual(1, len(transport.calls))

    def test_rate_limit_retry_is_counted_and_model_never_switches(self):
        self.budget(max_attempts=2)
        rate_limited = HTTPResponse(429, b"{}", {}, 5)
        transport = ScriptedTransport(
            [rate_limited, compatible_response()]
        )
        adapter = self.adapter(
            transport, config=self.remote_config(max_retries=1)
        )
        receipt = adapter.execute(self.request())
        self.assertEqual(2, receipt.telemetry["attempt_count"])
        self.assertEqual(1, receipt.telemetry["rate_limit_count"])
        models = {
            json.loads(call["body"])["model"] for call in transport.calls
        }
        self.assertEqual({"fixture-model"}, models)

    def test_provider_hard_budget_blocks_worst_case_reservation(self):
        self.budget(total=1.0, provider=0.00001)
        transport = ScriptedTransport([compatible_response()])
        with self.assertRaises(ExternalBudgetExceededError):
            self.adapter(transport).execute(self.request())
        self.assertEqual([], transport.calls)

    def test_logical_call_limit_is_hard(self):
        self.budget(max_calls=1)
        first_transport = ScriptedTransport([compatible_response()])
        self.adapter(first_transport).execute(self.request(key="one"))
        second_transport = ScriptedTransport([compatible_response()])
        with self.assertRaises(ExternalBudgetExceededError):
            self.adapter(second_transport).execute(
                self.request(key="two")
            )
        self.assertEqual([], second_transport.calls)

    def test_actual_billing_over_reservation_freezes_experiment(self):
        self.budget()
        response = compatible_response(cost_header=0.5)
        transport = ScriptedTransport([response])
        config = self.remote_config(billing_header="x-billed-usd")
        with self.assertRaises(ExternalBudgetExceededError):
            self.adapter(transport, config=config).execute(self.request())
        summary = self.store.experiment_summary("experiment_v0563")
        self.assertEqual("frozen", summary["status"])

    def test_secret_value_is_not_persisted_or_returned_by_config(self):
        secret = "sk-super-secret-fixture-value"
        os.environ["COSTWEAVE_TEST_SECRET"] = secret
        self.budget()
        transport = ScriptedTransport([compatible_response()])
        config = self.remote_config(secret_env="COSTWEAVE_TEST_SECRET")
        receipt = self.adapter(transport, config=config).execute(
            self.request()
        )
        persisted = self.store.call(receipt.external_call_id)
        self.assertNotIn(secret, json.dumps(persisted, sort_keys=True))
        self.assertNotIn(secret, json.dumps(config.public(), sort_keys=True))
        self.assertNotIn("secret_env", config.public())

    def test_text_adapter_rejects_tool_enabled_work_order(self):
        self.budget()
        transport = ScriptedTransport([compatible_response()])
        with self.assertRaises(ExperimentalCallError):
            self.adapter(transport).execute(
                self.request(work_order=self.work_order(tools=("shell",)))
            )
        self.assertEqual([], transport.calls)

    def test_batch_is_rejected_by_contract(self):
        with self.assertRaises(ExperimentalCallError):
            self.request(batch=True)

    def test_local_adapter_needs_only_experimental_switch(self):
        self.budget(provider_name="local-fixture")
        config = ExperimentalAdapterConfig(
            adapter_id="local-fixture",
            provider="local-fixture",
            endpoint="http://127.0.0.1:11434/generate",
            model_id="local-model",
            deployment="local",
            adapter_revision="local-http-v1",
        )
        transport = ScriptedTransport([local_response()])
        adapter = LocalHTTPAdapter(
            config,
            store=self.store,
            policy=self.enabled_policy(paid=False),
            transport=transport,
        )
        receipt = adapter.execute(self.request())
        self.assertEqual("local fixture", receipt.result.output["content"])
        self.assertEqual(1, len(transport.calls))

    def test_migration_and_runtime_schema_are_v0563(self):
        self.assertEqual("v5.6.5", RUNTIME_SCHEMA_VERSION)
        self.assertEqual(21, EventStore(self.path).health()["migrations"])
        self.assertIn("external_calls", self.store.counts())


class ShadowRouterTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        EventStore(self.path)
        self.calls = ExperimentalCallStore(self.path)
        self.calls.register_experiment(
            ExperimentBudget(
                experiment_id="experiment_shadow",
                total_cost_limit_usd=1.0,
                provider_cost_limits_usd={"fixture": 1.0},
                max_logical_calls=10,
                max_attempts_per_call=1,
            )
        )
        workers = (
            WorkerProfile(
                id="fixture-good",
                name="Fixture Good",
                specialty="coding",
                capabilities={"coding": 0.96, "safety": 0.9},
                cost_per_task=0.01,
                latency_factor=1.0,
                reliability=0.98,
                provider="fixture",
                reasoning=0.95,
                input_price_per_mtok=1.0,
                output_price_per_mtok=2.0,
                data_confidence=0.95,
            ),
            WorkerProfile(
                id="fixture-disabled",
                name="Fixture Disabled",
                specialty="coding",
                capabilities={"coding": 0.99},
                cost_per_task=0.0,
                latency_factor=0.8,
                reliability=0.99,
                provider="fixture",
                routable=False,
            ),
        )
        self.shadow = ShadowRouter(
            PredictiveRouter(workers=workers, catalog_revision="fixture-r1")
        )
        self.store = ShadowStore(self.path)
        self.task = TaskContract(
            id="task_shadow",
            title="Implement bounded function",
            objective="Implement and test one pure function",
            task_type="coding",
            required_capabilities=["coding"],
            acceptance_criteria=["tests pass"],
            estimated_input_tokens=1000,
            estimated_output_tokens=300,
            difficulty=3,
        )

    def tearDown(self):
        self.temp.cleanup()

    def decision(self, **changes):
        args = {
            "experiment_id": "experiment_shadow",
            "run_id": "run_shadow",
            "task": self.task,
            "actual_route": {"model_id": "actual-model"},
            "mode": RunMode.BALANCED,
            "quality_floor": 0.5,
        }
        args.update(changes)
        return self.shadow.observe(**args)

    def test_observation_does_not_mutate_task_or_actual_route(self):
        before = self.task.to_dict()
        actual = {"model_id": "actual-model", "locked": True}
        decision = self.decision(actual_route=actual)
        self.assertEqual(before, self.task.to_dict())
        self.assertEqual(
            {"model_id": "actual-model", "locked": True}, actual
        )
        self.assertFalse(
            decision.recommendation["applied_to_actual_route"]
        )
        self.assertTrue(decision.advisory_only)
        self.assertEqual(0, decision.intervention_count)

    def test_candidates_and_hard_rejections_are_recorded(self):
        decision = self.decision()
        self.assertEqual("fixture-good", decision.candidates[0]["model_id"])
        self.assertEqual(
            "fixture-disabled", decision.rejections[0]["model_id"]
        )

    def test_cost_ceiling_can_yield_no_recommendation(self):
        decision = self.decision(max_shadow_estimated_cost_usd=0.0)
        self.assertFalse(decision.recommendation["candidate_found"])
        self.assertIsNone(decision.recommendation["candidate"])

    def test_decision_persistence_is_immutable_and_idempotent(self):
        decision = self.decision()
        self.assertTrue(self.store.record_decision(decision))
        self.assertFalse(self.store.record_decision(decision))
        stored = self.store.decision(decision.shadow_decision_id)
        self.assertTrue(stored["advisory_only"])
        self.assertEqual(0, stored["intervention_count"])

    def test_comparison_never_claims_counterfactual_outcome(self):
        decision = self.decision()
        self.store.record_decision(decision)
        comparison = self.store.record_comparison(
            decision.shadow_decision_id,
            {
                "model_id": "actual-model",
                "accepted": True,
                "actual_cost_usd": 0.04,
                "actual_latency_ms": 900,
                "status": "completed",
                "evidence_level": "E2-real-unvalidated",
            },
        )
        self.assertFalse(comparison["counterfactual_outcome_known"])
        self.assertFalse(
            comparison["counterfactual_savings_claim_allowed"]
        )
        self.assertEqual(0, comparison["intervention_count"])

    def test_comparison_rejects_arbitrary_or_sensitive_fields(self):
        decision = self.decision()
        self.store.record_decision(decision)
        with self.assertRaises(ShadowIsolationError):
            self.store.record_comparison(
                decision.shadow_decision_id,
                {"model_id": "actual-model", "prompt": "secret"},
            )

    def test_shadow_evaluation_makes_zero_external_calls(self):
        decision = self.decision()
        self.store.record_decision(decision)
        self.assertEqual(0, self.calls.counts()["external_calls"])
        self.assertEqual(0, self.store.counts()["shadow_interventions"])


if __name__ == "__main__":
    unittest.main()
