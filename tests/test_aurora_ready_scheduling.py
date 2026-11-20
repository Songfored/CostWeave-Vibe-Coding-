from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from costweave import aurora_real_pilot as pilot
from costweave.aurora_rescue_eval import NODES, oracle_artifact
from costweave.event_store import EventStore
from costweave.experimental_calls import ExperimentBudget, ExperimentalCallStore


def accepted(node_id):
    if node_id in pilot.VIRTUAL_SPECS:
        key = "provider_plan_v2" if node_id == "provider_selection_v2" else node_id
        artifact = oracle_artifact("incident_replan")[key]
    else:
        artifact = oracle_artifact(node_id)
    return {"node_id": node_id, "status": "completed", "accepted": True}, artifact


class AuroraReadySchedulingTests(unittest.TestCase):
    def run_policy(self, execute, **kwargs):
        with patch.object(pilot, "_execute_node", side_effect=execute):
            return pilot._execute_policy(
                policy_id=pilot.ALL_PREMIUM, routes=pilot.freeze_routes()[pilot.ALL_PREMIUM],
                providers={}, store=object(), **kwargs,
            )

    def test_a1_b10_c6_crosses_stage_before_unrelated_b_finishes(self):
        # A=allocator patch (1), B=media package (10), C=code review (6).
        # C has one other dependency (tenant policy), which completes immediately.
        b_started = Event()
        c_finished = Event()
        costs = {"allocator_patch": 1, "media_package": 10, "code_review": 6}
        virtual_finish = {}

        def execute(node_id, **kwargs):
            if node_id == "allocator_patch":
                self.assertTrue(b_started.wait(3))
            elif node_id == "media_package":
                b_started.set()
                self.assertTrue(c_finished.wait(3), "C waited for unrelated B")
            if node_id in costs:
                dependency_time = max((virtual_finish.get(dep, 0) for dep in kwargs["dependencies"]), default=0)
                virtual_finish[node_id] = dependency_time + costs[node_id]
            if node_id == "code_review":
                c_finished.set()
            if node_id in pilot.REAL_NODES and pilot.REAL_NODES[node_id].stage < 7:
                self.assertFalse(kwargs["reveal_incident"])
                self.assertNotIn("incident_replan", kwargs["all_artifacts"])
            return accepted(node_id)

        result = self.run_policy(execute)
        self.assertEqual(7, virtual_finish["code_review"])
        self.assertEqual(10, max(virtual_finish.values()))
        by_id = {node["node_id"]: node for node in result["nodes"]}
        self.assertLessEqual(by_id["allocator_patch"]["finished_at_ms"], by_id["code_review"]["started_at_ms"])
        self.assertLess(by_id["code_review"]["started_at_ms"], by_id["media_package"]["finished_at_ms"])
        for node in NODES:
            if node.stage < 7:
                self.assertLessEqual(by_id[node.node_id]["finished_at_ms"], by_id["incident_replan"]["started_at_ms"])
        self.assertTrue(result["accepted"])
        self.assertEqual(28, len(result["nodes"]))

    def test_virtual_review_starts_on_its_own_branch_and_final_waits_for_both(self):
        deployment_started = Event()
        financial_started = Event()

        def execute(node_id, **kwargs):
            if node_id == "provider_selection_v2":
                self.assertTrue(deployment_started.wait(3))
            if node_id == "deployment_plan_v2":
                deployment_started.set()
                self.assertTrue(financial_started.wait(3), "virtual review waited for the other revision")
            if node_id == "financial_review_v2":
                self.assertIn("provider_selection_v2", kwargs["dependencies"])
                self.assertNotIn("deployment_plan_v2", kwargs["dependencies"])
                financial_started.set()
            if node_id == "final_assembly":
                self.assertTrue(pilot.VIRTUAL_SPECS.keys() <= kwargs["all_artifacts"].keys())
            return accepted(node_id)

        self.assertTrue(self.run_policy(execute)["accepted"])

    def test_failed_virtual_branch_blocks_final_and_preserves_independent_review(self):
        def execute(node_id, **kwargs):
            if node_id == "deployment_plan_v2":
                return {"node_id": node_id, "accepted": False, "status": "failed"}, None
            return accepted(node_id)

        preincident = {n.node_id: oracle_artifact(n.node_id) for n in NODES}
        result = self.run_policy(execute, seed_artifacts=preincident)
        by_id = {node["node_id"]: node for node in result["nodes"]}
        self.assertEqual("completed", by_id["financial_review_v2"]["status"])
        self.assertEqual(["deployment_plan_v2"], by_id["security_review_v2"]["missing_dependencies"])
        self.assertEqual(["incident_replan"], by_id["final_assembly"]["missing_dependencies"])
        self.assertIsNone(result["final_output"])
        self.assertFalse(result["accepted"])

    def test_admission_retries_deferred_nodes_only_after_inflight_can_settle(self):
        fast_finished = Event()
        gate_seen = Event()
        original = pilot._admit_runtime_wave
        nodes = ("allocator_patch", "provider_selection", "media_package")
        def admit(**kwargs):
            admitted, _, plans = original(**kwargs)
            if "media_package" in admitted and not gate_seen.is_set():
                admitted.remove("media_package")
                if fast_finished.is_set():
                    gate_seen.set()
                return admitted, ["media_package"], plans
            return admitted, [], plans

        def execute(node_id, **kwargs):
            if node_id == "allocator_patch":
                fast_finished.set()
            if node_id == "provider_selection":
                self.assertTrue(gate_seen.wait(3))
            return accepted(node_id)

        with patch.object(pilot, "_admit_runtime_wave", side_effect=admit):
            result = self.run_policy(execute)
        self.assertTrue(result["accepted"])
        report = next(stage for stage in result["stages"] if stage["stage"] == 5)
        self.assertTrue(any("media_package" in d["deferred"] for d in report["budget_deferrals"]))
        self.assertEqual(set(nodes), set(nodes).intersection(report["runnable"]))

    def test_budget_counts_dispatched_primary_exactly_once_before_and_after_reservation(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "ledger.sqlite3"
            EventStore(path)
            store = ExperimentalCallStore(path)
            store.register_experiment(ExperimentBudget(
                experiment_id=pilot.EXPERIMENT_ID, total_cost_limit_usd=1.0,
                provider_cost_limits_usd={"p": 0.8, "q": 1.0}, max_logical_calls=2,
                max_attempts_per_call=1,
            ))
            pending = {"task_a": ("p", 0.6)}
            def headroom(provider):
                snapshot = store.budget_headroom(
                    pilot.EXPERIMENT_ID, provider=provider, pending_reservations=pending,
                )
                return (snapshot["total_remaining_usd"], snapshot["provider_remaining_usd"],
                        snapshot["logical_calls_remaining"])

            self.assertEqual((0.4, 0.2, 1), headroom("p"))
            self.assertEqual((0.4, 1.0, 1), headroom("q"))
            store.reserve(
                experiment_id=pilot.EXPERIMENT_ID, run_id="run_test", task_id="task_a",
                deployment_id="deployment_test", provider="p", model_id="test",
                adapter_revision="test", reasoning_effort="low", idempotency_key="primary-a",
                request_fingerprint={}, worst_case_cost_per_attempt_usd=0.6, max_attempts=1,
            )
            self.assertEqual((0.4, 0.2, 1), headroom("p"))
            self.assertEqual((0.4, 1.0, 1), headroom("q"))

    def test_terminal_failure_and_budget_limit_do_not_block_unrelated_work(self):
        original = pilot._admit_runtime_wave
        def admit(**kwargs):
            allowed, _, plans = original(**kwargs)
            if "media_package" in allowed:
                return [n for n in allowed if n != "media_package"], ["media_package"], plans
            return allowed, [], plans

        def execute(node_id, **kwargs):
            self.assertNotEqual("allocator_patch", node_id)
            return accepted(node_id)

        with patch.object(pilot, "_admit_runtime_wave", side_effect=admit):
            result = self.run_policy(execute, terminal_failures={"allocator_patch": "prior terminal failure"})
        by_id = {row["node_id"]: row for row in result["nodes"]}
        self.assertEqual("skipped_learned_terminal_failure", by_id["allocator_patch"]["status"])
        self.assertEqual("blocked_budget_hard_cap", by_id["media_package"]["status"])
        self.assertEqual("skipped_missing_dependencies", by_id["code_review"]["status"])
        self.assertEqual("completed", by_id["financial_review"]["status"])
        self.assertFalse(result["accepted"])

    def test_verified_matching_checkpoints_skip_calls_and_do_not_leak_future_outputs(self):
        seeds = {node.node_id: oracle_artifact(node.node_id) for node in NODES}
        def no_calls(**kwargs):
            self.fail("validated checkpoint unexpectedly reran")

        result = self.run_policy(no_calls, seed_artifacts=seeds, virtual_revisions_verified=True)
        self.assertEqual(24, result["reused_checkpoint_artifacts"])
        self.assertTrue(result["virtual_reverification_skipped"])
        self.assertTrue(result["accepted"])
