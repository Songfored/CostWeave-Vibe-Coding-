from __future__ import annotations

import tempfile
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from costweave.decision_bridge import (
    DECISION_BRIDGE_SCHEMA_VERSION,
    DecisionOrchestratorV1,
    failure_closed_envelope,
    MODULE_ORDER,
)
from costweave.domain import RunRequest, RunStatus
from costweave.engine import RunManager


class _BrokenAdapter:
    def decide(self, payload, idempotency_key):
        raise RuntimeError("injected advisory failure")


class DecisionBridgeV069Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = str(Path(self.temp.name) / "runtime.sqlite3")

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _wait(record, timeout: float = 15.0):
        deadline = time.time() + timeout
        while record.status not in {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        } and time.time() < deadline:
            time.sleep(0.02)
        return record

    def _run(self, manager: RunManager | None = None):
        manager = manager or RunManager(runtime_db_path=self.db, recover=False)
        record = manager.create(
            RunRequest(
                goal="实现一个带单元测试的本地网页组件",
                budget=1.0,
                max_concurrency=3,
            )
        )
        self._wait(record)
        thread = manager.threads.get(record.id)
        if thread is not None:
            thread.join(timeout=5)
        return manager, record

    def test_all_five_modules_share_one_run_and_trace_without_authority(self):
        _, record = self._run()
        self.assertEqual(RunStatus.COMPLETED, record.status)
        envelope = record.decision_envelope
        self.assertIsNotNone(envelope)
        self.assertEqual(DECISION_BRIDGE_SCHEMA_VERSION, envelope["schema_version"])
        self.assertEqual(record.id, envelope["run_id"])
        self.assertEqual(record.trace_id, envelope["trace_id"])
        self.assertFalse(envelope["failure_closed"])
        self.assertEqual(list(MODULE_ORDER), [item["module"] for item in envelope["module_receipts"]])
        for item in envelope["module_receipts"]:
            if item["module"] == "safe_learning":
                self.assertEqual("insufficient_evidence", item["status"])
                self.assertEqual(0, item["decision"]["observation_count"])
            else:
                self.assertEqual("ok", item["status"])
        self.assertEqual(
            {"formal_route": 0, "budget": 0, "task_status": 0, "dag_mutations": 0,
             "provider_calls": 0, "network_calls": 0, "external_calls": 0},
            envelope["effects"],
        )

    def test_baseline_and_shadow_use_exact_same_frozen_input(self):
        _, record = self._run()
        envelope = record.decision_envelope
        self.assertEqual(envelope["shared_input_fingerprint"], envelope["baseline_input_fingerprint"])
        self.assertEqual(envelope["shared_input_fingerprint"], envelope["shadow_input_fingerprint"])
        self.assertEqual([], envelope["structured_diff"]["input_diff"])
        self.assertFalse(envelope["structured_diff"]["applied"])
        self.assertTrue(envelope["baseline_route"]["applied"])
        self.assertFalse(envelope["shadow_route"]["applied"])

    def test_semantic_fingerprint_is_deterministic_and_envelope_is_frozen(self):
        manager, record = self._run()
        orchestrator = DecisionOrchestratorV1(manager.control_kernel)
        first_object = orchestrator.build(record)
        second_object = orchestrator.build(record)
        self.assertEqual(first_object.fingerprint, second_object.fingerprint)
        with self.assertRaises(FrozenInstanceError):
            second_object.fingerprint = "changed"

    def test_module_failure_closes_to_baseline_and_run_still_completes(self):
        manager = RunManager(runtime_db_path=self.db, recover=False)
        manager.engine.decision_orchestrator.adapters["joint_search"] = _BrokenAdapter()
        _, record = self._run(manager)
        self.assertEqual(RunStatus.COMPLETED, record.status)
        envelope = record.decision_envelope
        self.assertTrue(envelope["failure_closed"])
        for failed in (envelope, failure_closed_envelope(record, "setup failed").to_dict()):
            self.assertTrue(failed["baseline_route"]["tasks"])
            self.assertIsNone(failed["shadow_route"]["tasks"])
            self.assertIsNone(failed["shadow_route"]["estimated_total_cost_usd"])
            self.assertFalse(failed["structured_diff"]["cost_comparable"])
        receipt = next(item for item in envelope["module_receipts"] if item["module"] == "joint_search")
        self.assertEqual("failed_closed", receipt["status"])
        self.assertIn("injected advisory failure", receipt["error"])
        self.assertEqual(0, envelope["effects"]["formal_route"])

    def test_shadow_uses_the_selected_workers_own_quote_and_preserves_unknown(self):
        option = {"route_id": "t:second", "executor_id": "second", "cost_known": True,
                  "predicted_cost_usd": {"mean": 0.25}}
        routes = [{"node_id": "t", "options": [option]}]
        candidate = {"candidate_id": "plan", "strategy": {"management_mode": "no_delegation"},
                     "node_routes": [["t", "t:second"]]}
        decisions = {"meta_controller": {"decision": {"decision": {"action": "fast"}}},
                     "joint_search": {"decision": {"decision": {"action": "SELECT", "selected_candidate_id": "plan",
                                                                 "selected_candidate": candidate}}},
                     "capability_graph": {"decision": {}}}
        baseline = {"management_mode": "planner", "estimated_total_cost_usd": 1.0,
                    "tasks": [{"task_id": "t", "worker_id": "first"}]}
        shadow = DecisionOrchestratorV1._shadow_route(decisions, routes, failure_closed=False)
        self.assertEqual(0.25, shadow["estimated_total_cost_usd"])
        self.assertEqual("no_delegation", shadow["management_mode"])
        changes = DecisionOrchestratorV1._route_diff(baseline, shadow)
        self.assertEqual({"worker_id", "management_mode", "estimated_total_cost_usd"}, {row["field"] for row in changes})
        option["cost_known"] = False
        shadow = DecisionOrchestratorV1._shadow_route(decisions, routes, failure_closed=False)
        self.assertIsNone(shadow["estimated_total_cost_usd"])
        self.assertNotIn("estimated_total_cost_usd", [row["field"] for row in DecisionOrchestratorV1._route_diff(baseline, shadow)])
        candidate["node_routes"] = []
        self.assertIsNone(DecisionOrchestratorV1._shadow_route(decisions, routes, failure_closed=False)["tasks"])

    def test_envelope_round_trips_through_persistent_projection(self):
        manager, record = self._run()
        expected = record.decision_envelope
        reloaded = RunManager(runtime_db_path=self.db, recover=False)
        hydrated = reloaded.get(record.id)
        self.assertIsNotNone(hydrated)
        self.assertEqual(expected, hydrated.decision_envelope)
        self.assertEqual(expected, reloaded.decision_bridge(record.id))

    def test_management_depth_is_bounded_at_two(self):
        _, record = self._run()
        envelope = record.decision_envelope
        self.assertEqual(2, envelope["management_depth_limit"])
        self.assertTrue(
            all(item["management_depth"] <= 2 for item in envelope["candidate_management_topologies"])
        )


if __name__ == "__main__":
    unittest.main()
