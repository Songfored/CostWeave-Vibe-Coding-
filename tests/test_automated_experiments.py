import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from costweave.automated_experiments import AutomatedExperiments, FixtureTransport
from costweave.automation_tasks import experiment_tasks
from costweave.experiment_lab import ExperimentLab, exact_result
from costweave.experimental_calls import ExperimentBudget, ExperimentalCallStore, ExternalBudgetExceededError
from costweave.event_store import EventStore
from costweave.adapters import ExperimentalAdapterConfig, HTTPResponse
from costweave.experiment_lab import ResponsesLabAdapter, endpoint


class AutomationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.lab = ExperimentLab(self.root)
        self.sent = []
        def factory(task):
            self.sent.append(task["id"])
            return FixtureTransport(task["expected"])
        self.auto = AutomatedExperiments(self.lab, transport_factory=factory)
        self.lab.automation = self.auto

    def tearDown(self):
        self.wait()
        self.tmp.cleanup()

    def wait(self):
        worker = self.auto.worker
        if worker is not None:
            worker.join(25)
            self.assertFalse(worker.is_alive(), "worker did not finish")

    def connect(self, **kwargs):
        result = self.auto.save_and_enqueue({"label": "fixture source", "endpoint": "https://example.com/v1",
            "model": "fixture-model", "profile": "compatible", "api_key": "PRIVATE_AUTOMATION_TEST_VALUE",
            "auto_run": True, **kwargs})
        self.wait()
        return result

    def bill(self, identity, **kwargs):
        return self.auto.invoice({"round_id": identity, "amount": "0.08", "currency": "CNY",
            "source_reference": "synthetic bill fixture", "scope_confirmed": True, **kwargs})

    def test_one_api_save_runs_eight_calls_no_price_required(self):
        result = self.connect()
        row = self.auto.rounds[result["id"]]
        self.assertEqual("completed", row["status"], row)
        self.assertEqual(8, row["summary"]["passed"])
        self.assertEqual(8, len(self.sent))
        self.assertIsNone(row["monetary_limit"])
        self.assertFalse(row["production_routing_changed"])
        self.assertIsNone(self.auto.status()["rounds"][0]["cost"]["actual_amount"])
        path = self.auto.root / "rounds" / row["id"]
        self.assertNotIn("PRIVATE_AUTOMATION_TEST_VALUE", (path / "report.json").read_text())
        self.assertNotIn("PRIVATE_AUTOMATION_TEST_VALUE", (path / "connection.private.json").read_text())
        store = ExperimentalCallStore(path / "runtime.sqlite3")
        self.assertIsNone(store.experiment_summary("experiment_"+row["id"])["actual_cost_usd"])
        call = store.call(row["calls"][0]["external_call_id"])
        self.assertIsNone(call["actual_cost"])
        self.assertIsNone(call["result"]["result"]["actual_cost"])
        self.assertIsNone(call["telemetry"]["actual_cost_usd"])

    def test_repeated_save_deduplicates_even_without_returned_id(self):
        first = self.connect()
        second = self.connect()
        self.assertEqual(first["id"], second["id"])
        self.assertTrue(second["deduplicated"])
        self.assertEqual(8, len(self.sent))
        third = self.connect(id=first["connection_id"], api_key="", label="renamed")
        self.assertEqual(first["id"], third["id"])

    def test_offline_full_chain_never_constructs_remote_transport(self):
        with patch("costweave.adapters.http.UrllibHTTPTransport", side_effect=AssertionError("network")):
            result = self.auto.enqueue(mode="offline")
            self.wait()
        row = self.auto.rounds[result["id"]]
        self.assertEqual("completed", row["status"], row)
        self.assertEqual(8, row["summary"]["passed"])
        self.assertTrue(all(c["usage_synthetic"] for c in row["calls"]))
        with self.assertRaises(ValueError):
            self.bill(row["id"])

    def test_no_unbounded_retry_on_failure_or_missing_usage(self):
        class Failing(FixtureTransport):
            def post(self, **kwargs):
                raise ValueError("PRIVATE_AUTOMATION_TEST_VALUE")
        attempts = []
        def failure(task):
            attempts.append(task["id"])
            return Failing(task["expected"])
        self.auto.transport_factory = failure
        result = self.connect()
        row = self.auto.rounds[result["id"]]
        self.assertEqual("failed", row["status"])
        self.assertEqual(1, len(attempts))
        self.assertEqual(7, row["summary"]["unrun"])
        self.assertNotIn("PRIVATE_AUTOMATION_TEST_VALUE", json.dumps(row))
        invoice = self.bill(row["id"])
        self.assertIsNone(invoice["calibration"])
        self.assertEqual("0.0800000000", self.auto.status()["rounds"][0]["cost"]["actual_amount"])

    def test_bill_corrects_total_and_next_round_uses_frozen_calibration(self):
        first = self.connect()
        recorded = self.bill(first["id"])
        calibration = recorded["calibration"]
        self.assertEqual(1, calibration["sample_rounds"])
        self.assertEqual(1040, calibration["sample_tokens"])
        second = self.auto.enqueue(first["connection_id"])
        self.wait()
        row = self.auto.rounds[second["id"]]
        self.assertEqual(calibration, row["calibration_snapshot"])
        self.assertAlmostEqual(.08, float(row["estimated_cost"]["amount"]))
        feedback = self.bill(second["id"], amount="0.12", source_reference="second fixture")
        self.assertEqual(2, feedback["calibration"]["sample_rounds"])
        enriched = next(r for r in self.auto.status()["rounds"] if r["id"] == second["id"])
        self.assertAlmostEqual(.04, float(enriched["cost"]["residual"]))
        self.assertAlmostEqual(.08, float(row["estimated_cost"]["amount"]))
        self.assertTrue((self.auto.root / "rounds" / second["id"] / "reconciled-report.json").exists())

    def test_duplicate_bill_and_explicit_revision_are_auditable(self):
        result = self.connect()
        first = self.bill(result["id"])["invoice"]
        duplicate = self.bill(result["id"])["invoice"]
        self.assertEqual(first, duplicate)
        self.assertEqual(1, len(self.auto.book.history))
        with self.assertRaises(ValueError):
            self.bill(result["id"], amount="0.1")
        second = self.bill(result["id"], amount="0.1", supersedes=first["id"])["invoice"]
        self.assertEqual(first["id"], second["supersedes"])
        self.assertEqual(2, len(self.auto.book.history))
        self.assertEqual(first, self.auto.book.history[0])
        revised = self.bill(result["id"], supersedes=second["id"])["invoice"]
        self.assertNotEqual(first["id"], revised["id"])

    def test_points_not_converted_and_model_changes_do_not_reuse_rates(self):
        first = self.connect()
        cal = self.bill(first["id"], currency="POINTS", amount="0.002")["calibration"]
        self.assertEqual("POINTS", cal["currency"])
        second = self.connect(id=first["connection_id"], api_key="", model="another-model")
        self.assertIsNone(self.auto.rounds[second["id"]]["calibration_snapshot"])
        self.assertNotEqual(self.auto.rounds[first["id"]]["calibration_scope"], self.auto.rounds[second["id"]]["calibration_scope"])

    def test_bad_bills_do_not_pollute_calibration(self):
        result = self.connect()
        for update in ({"amount": "NaN"}, {"amount": "Infinity"}, {"amount": -1}, {"amount": True},
                       {"currency": "EUR"}, {"scope_confirmed": False}, {"source_reference": ""}):
            with self.subTest(update=update), self.assertRaises(ValueError):
                self.bill(result["id"], **update)
        self.assertFalse(self.auto.book.history)
        self.assertEqual("0.0000000000", self.bill(result["id"], amount="0")["invoice"]["amount"])

    def test_cancel_finishes_current_call_without_next_call(self):
        entered, release = threading.Event(), threading.Event()
        class Paused(FixtureTransport):
            def post(self, **kwargs):
                entered.set()
                release.wait(10)
                return super().post(**kwargs)
        self.auto.transport_factory = lambda task: Paused(task["expected"])
        result = self.auto.save_and_enqueue({"label": "paused", "endpoint": "https://example.com/v1",
            "model": "test", "api_key": "PRIVATE_AUTOMATION_TEST_VALUE", "auto_run": True})
        self.assertTrue(entered.wait(10))
        self.auto.cancel(result["id"])
        release.set()
        self.wait()
        row = self.auto.rounds[result["id"]]
        self.assertEqual("cancelled", row["status"])
        self.assertEqual(1, len(row["calls"]))

    def test_restart_preserves_history_and_does_not_replay(self):
        result = self.connect()
        self.bill(result["id"])
        interrupted = {**self.auto.rounds[result["id"]], "id": "auto_interrupted", "status": "running"}
        self.auto._persist(interrupted)
        recovered = AutomatedExperiments(self.lab)
        self.assertEqual("interrupted", recovered.rounds["auto_interrupted"]["status"])
        self.assertIsNone(recovered.worker)
        self.assertEqual(1, len(recovered.book.history))

    def test_reference_tasks_are_stable_and_nested_types_are_strict(self):
        self.assertEqual(experiment_tasks(), experiment_tasks())
        self.assertEqual(8, len(experiment_tasks()))
        for task in experiment_tasks():
            self.assertTrue(exact_result(json.dumps(task["expected"]), task["expected"])["accepted"])
        self.assertFalse(exact_result('{"values":[true]}', {"values": [1]})["accepted"])
        tasks = {t["id"]: t for t in experiment_tasks()}
        for category in ("constraints", "evidence"):
            self.assertEqual(tasks[category+"_0"]["schema"], tasks[category+"_1"]["schema"])

    def test_responses_interface_runs_through_same_ledger(self):
        payloads = []
        class ResponsesTransport(FixtureTransport):
            def post(self, **kwargs):
                payloads.append(json.loads(kwargs["body"]))
                value = {"status": "completed", "id": "resp-fixture", "model": "fixture",
                         "output": [{"type": "reasoning", "summary": []},
                                    {"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.expected)}]}],
                         "usage": {"input_tokens": 100, "output_tokens": 40,
                                   "input_tokens_details": {"cached_tokens": 10},
                                   "output_tokens_details": {"reasoning_tokens": 20}}}
                return HTTPResponse(200, json.dumps(value).encode(), {}, 1)
        self.auto.transport_factory = lambda task: ResponsesTransport(task["expected"])
        result = self.connect(profile="responses")
        row = self.auto.rounds[result["id"]]
        self.assertEqual("completed", row["status"], row)
        self.assertEqual(8, len(payloads))
        self.assertEqual(1024, payloads[0]["max_output_tokens"])
        self.assertFalse(payloads[0]["store"])
        self.assertNotIn("messages", payloads[0])
        self.assertEqual(20, row["calls"][0]["usage"]["reasoning_tokens"])
        self.assertEqual("https://example.com/v1/responses", row["connection"]["endpoint"])

    def test_responses_incomplete_or_missing_usage_is_not_success(self):
        path = self.root / "parse.sqlite3"
        EventStore(path)
        adapter = ResponsesLabAdapter(ExperimentalAdapterConfig("test", "p", "https://example.com/v1/responses", "model", "remote"),
                                      store=ExperimentalCallStore(path))
        for value in ({"status": "incomplete", "output": []},
                      {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "{}"}]}]}):
            with self.assertRaises(Exception):
                adapter.parse_response(value)
        with self.assertRaises(ValueError):
            endpoint("https://example.com/v1/chat/completions", "responses")


class UsagePolicyTests(unittest.TestCase):
    def test_usage_mode_is_explicit_and_does_not_disable_priced_budgets(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/"runtime.sqlite3"
            EventStore(path)
            store = ExperimentalCallStore(path)
            with self.assertRaises(Exception):
                ExperimentBudget("experiment_no_mode", 0, {"provider": 0}, 1, 1)
            budget = ExperimentBudget("experiment_usage", 0, {"provider": 0}, 1, 1, accounting_mode="usage_only")
            store.register_experiment(budget)
            values = dict(experiment_id="experiment_usage", run_id="run", task_id="task", deployment_id="deployment",
                          provider="provider", model_id="model", adapter_revision="revision", reasoning_effort="low",
                          idempotency_key="key-first", request_fingerprint={"x": 1}, worst_case_cost_per_attempt_usd=0, max_attempts=1)
            with self.assertRaises(ExternalBudgetExceededError):
                store.reserve(**values)
            store.reserve(**values, accounting_mode="usage_only")
            with self.assertRaises(ExternalBudgetExceededError):
                store.reserve(**{**values, "idempotency_key": "second"}, accounting_mode="usage_only")
            self.assertIsNone(store.budget_headroom("experiment_usage", provider="provider")["total_remaining_usd"])
            self.assertNotIn("accounting_mode", ExperimentBudget("experiment_old", 1, {"p": 1}, 1, 1).to_dict())


if __name__ == "__main__":
    unittest.main()
