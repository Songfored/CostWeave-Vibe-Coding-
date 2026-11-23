import json
import tempfile
import unittest
from pathlib import Path

from costweave.adapters import AdapterRuntimePolicy, ExperimentalAdapterConfig, HTTPResponse, OpenAICompatibleHTTPAdapter
from costweave.event_store import EventStore
from costweave.experimental_calls import ExperimentBudget, ExperimentalCallStore
from costweave.kernel.contracts import Job, Operation
from costweave.kernel.ports import AdmissionDenied, ExecutionResult, KnownFailure, default_registry
from costweave.kernel.provider_port import register_model_route
from costweave.kernel.runtime import Runtime
from costweave.kernel.store import KernelStore


class KernelPortsTests(unittest.TestCase):
    def test_pure_ports_and_independent_final_contract(self):
        registry = default_registry()
        job = Job(("field",), {"input": {"rows": [0, {"answer": False}]}}, 1, (
            Operation("select", ("input",), "field", "pick", "present", 1,
                      {"source": "input", "path": ["rows", 1, "answer"]}),
        ), acceptance={"field": {"validator": "equals", "config": {"expected": False}}})
        registry.check(job)
        op = job.operations[0]
        value = registry.execute(op, job.inputs, attempt_id="1")
        self.assertIs(value, False)
        self.assertTrue(registry.validate(op, value, job.inputs))
        self.assertTrue(registry.accept("field", job.acceptance["field"], value))
        self.assertFalse(registry.accept("field", job.acceptance["field"], 0))
        collect = Operation("assemble", ("input",), "package", "collect", "json_object", 0, {"required_keys": ["input"]})
        packed = registry.execute(collect, {**job.inputs, "unrelated": "omit"}, attempt_id="2")
        self.assertEqual(job.inputs, packed)
        self.assertTrue(registry.validate(collect, packed, job.inputs))
        self.assertFalse(registry.validate(collect, {}, job.inputs))

    def test_check_resolves_all_validators_before_any_executor_runs(self):
        registry = default_registry()
        op = Operation("constant", (), "field", "literal", "present", 0, {"value": 9})
        for rule in ({"validator": "absent", "config": {}},
                     {"validator": "equals", "config": {}},
                     {"validator": "json_object", "config": {"required_keys": 1}}):
            with self.subTest(rule=rule), self.assertRaises(ValueError):
                registry.check(Job(("field",), {}, 0, (op,), acceptance={"field": rule}))
        with self.assertRaises(ValueError):
            registry.register_executor("literal", lambda *a, **kw: 0)

    def test_host_plugins_only_receive_declared_copies_and_boolean_verdicts(self):
        registry = default_registry()
        def producer(op, inputs, *, attempt_id):
            self.assertEqual({"x"}, inputs.keys())
            inputs["x"].append(attempt_id)
            return ExecutionResult(inputs["x"], 1, "record")
        def mutating_validator(op, value, inputs):
            inputs["x"].clear()
            value.clear()
            return "true"
        registry.register_executor("producer", producer)
        registry.register_validator("verify", mutating_validator)
        op = Operation("produce", ("x",), "y", "producer", "verify", 2)
        inputs = {"x": [3], "private": "omit"}
        receipt = registry.perform(op, inputs, attempt_id="a")
        with self.assertRaises(TypeError):
            registry.validate(op, receipt.value, inputs)
        self.assertEqual(([3, "a"], 1, "record"), (receipt.value, receipt.actual_units, receipt.receipt_id))
        self.assertEqual([3], inputs["x"])

    def test_invalid_result_and_settlement_have_distinct_recovery_semantics(self):
        registry = default_registry()
        registry.register_executor("nan", lambda op, inputs, *, attempt_id: ExecutionResult(float("nan"), 1))
        registry.register_executor("overrun", lambda op, inputs, *, attempt_id: ExecutionResult("value", 9))
        with self.assertRaises(KnownFailure) as error:
            registry.perform(Operation("a", (), "r", "nan", "present", 2), {}, attempt_id="a")
        self.assertEqual(1, error.exception.actual_units)
        with self.assertRaises(ValueError):
            registry.perform(Operation("b", (), "r", "overrun", "present", 2), {}, attempt_id="b")


class _FixtureTransport:
    evidence_level = "E0-transport-fixture"

    def __init__(self, response, billed=None, with_usage=True):
        self.response = response
        self.billed = billed
        self.with_usage = with_usage
        self.payloads = []

    def post(self, *, url, headers, body, timeout_seconds):
        self.payloads.append(json.loads(body))
        if isinstance(self.response, Exception):
            raise self.response
        payload = {"choices": [{"message": {"content": self.response}}]}
        if self.with_usage:
            payload["usage"] = {"prompt_tokens": 100, "completion_tokens": 10}
        return HTTPResponse(200, json.dumps(payload).encode(), {"x-billed": self.billed} if self.billed else {}, 3)


class KernelProviderPortTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        ledger_path = Path(temp.name) / "external.db"
        EventStore(ledger_path)
        self.ledger = ExperimentalCallStore(ledger_path)
        self.registry = default_registry()
        self.runtime = Runtime(KernelStore(Path(temp.name) / "kernel.db"), self.registry)

    def budget(self, cap=1.0):
        self.ledger.register_experiment(ExperimentBudget(
            "experiment_kernel", cap, {"fixture": cap}, max_logical_calls=4, max_attempts_per_call=1,
        ))

    def route(self, name, response, *, enabled=True, billed=None, with_usage=True):
        transport = _FixtureTransport(response, billed, with_usage)
        config = ExperimentalAdapterConfig(
            name, "fixture", "https://example.invalid/v1/chat/completions", name, "remote",
            input_price_per_mtok_usd=1, output_price_per_mtok_usd=2,
            billing_header="x-billed",
        )
        adapter = OpenAICompatibleHTTPAdapter(config, store=self.ledger, transport=transport,
                                              policy=AdapterRuntimePolicy(enabled, enabled))
        register_model_route(self.registry, name, adapter, experiment_id="experiment_kernel", run_id="run-kernel",
                             max_output_tokens=64, usd_per_unit=.01)
        return transport

    def job(self):
        return Job(("answer",), {"question": "twice two", "unrelated": "private-fixture"}, 5, (
            Operation("quick", ("question",), "answer", "small", "present", 2, {"prompt": "Compute the answer"}),
            Operation("second", ("question",), "answer", "large", "present", 3, {"prompt": "Compute the answer"}),
        ), acceptance={"answer": {"validator": "equals", "config": {"expected": {"result": 4}}}})

    def test_two_models_through_runtime_and_actual_usd_ledger(self):
        self.budget()
        first = self.route("small", '{"result":0}', billed="0.00012")
        second = self.route("large", '{"result":4}', billed="0.00012")
        state = self.runtime.submit("run-kernel", self.job())
        self.assertEqual(("complete", {"result": 4}, 2, 0), (
            state["status"], state["artifacts"]["answer"], state["spent_units"], state["held_units"],
        ))
        self.assertEqual((1, 1), (len(first.payloads), len(second.payloads)))
        self.assertEqual("large", second.payloads[0]["model"])
        self.assertNotIn("private-fixture", json.dumps(second.payloads))
        self.assertIn("twice two", json.dumps(first.payloads))
        summary = self.ledger.experiment_summary("experiment_kernel")
        self.assertEqual(2, summary["logical_calls"])
        self.assertAlmostEqual(.00024, summary["actual_cost_usd"])
        self.runtime.resume("run-kernel")
        self.assertEqual(1, len(second.payloads))

    def test_duplicate_provider_ticket_replays_one_receipt(self):
        self.budget()
        transport = self.route("small", '{"result":4}')
        op = self.job().operations[0]
        args = (op, {"question": "twice two"})
        first = self.registry.perform(*args, attempt_id="same")
        second = self.registry.perform(*args, attempt_id="same")
        self.assertEqual(first, second)
        self.assertTrue(first.receipt_id.startswith("external_call_"))
        self.assertEqual(1, len(transport.payloads))

    def check_quote_is_charged(self, with_usage):
        self.budget()
        self.route("small", '{"result":4}', with_usage=with_usage)
        self.route("large", "0")
        state = self.runtime.submit("run-kernel", self.job())
        self.assertEqual(("complete", 2, 0), (state["status"], state["spent_units"], state["held_units"]))
        self.assertEqual(1, self.ledger.experiment_summary("experiment_kernel")["logical_calls"])

    def test_paid_response_without_usage_consumes_full_kernel_quote(self):
        self.check_quote_is_charged(with_usage=False)

    def test_model_usage_price_estimate_also_consumes_full_kernel_quote(self):
        self.check_quote_is_charged(with_usage=True)

    def test_disabled_or_unfunded_model_stops_without_substitution(self):
        self.budget(cap=.000001)
        first = self.route("small", "4", enabled=False)
        other = self.route("large", "4")
        result = self.runtime.submit("run-kernel", self.job())
        self.assertEqual(("blocked", 0), (result["status"], result["spent_units"]))
        with self.assertRaises(AdmissionDenied):
            self.registry.execute(self.job().operations[1], self.job().inputs, attempt_id="unfunded")
        self.assertEqual(([], []), (first.payloads, other.payloads))

    def test_ambiguous_transport_waits_for_reconciliation(self):
        self.budget()
        first = self.route("small", TimeoutError("fixture"))
        second = self.route("large", "4")
        result = self.runtime.submit("run-kernel", self.job())
        self.assertEqual("needs_reconciliation", result["status"])
        self.assertEqual(2, result["held_units"])
        self.runtime.resume("run-kernel")
        self.assertEqual((1, 0), (len(first.payloads), len(second.payloads)))

    def test_overrun_after_transport_preserves_hold_and_freezes_provider_budget(self):
        self.budget()
        self.route("small", "4", billed="0.9")
        other = self.route("large", "4")
        result = self.runtime.submit("run-kernel", self.job())
        self.assertEqual(("needs_reconciliation", 2), (result["status"], result["held_units"]))
        self.assertEqual("frozen", self.ledger.experiment_summary("experiment_kernel")["status"])
        self.assertEqual([], other.payloads)
