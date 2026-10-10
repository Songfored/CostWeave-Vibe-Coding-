import copy
import tempfile
import threading
import time
import unittest
from pathlib import Path

from costweave.aurora_rescue_eval import (
    AURORA_TASK_VERSION,
    FallbackCandidate,
    NODES,
    STAGES,
    allocator_test_results,
    decide_budgeted_fallback,
    frozen_task_record,
    oracle_artifact,
    public_task,
    run_adapter_concurrency_gate,
    run_fault_matrix,
    run_offline_suite,
    run_oracle_preflight,
    sealed_oracle_digest,
    tenant_test_results,
    validate_node,
)
from costweave.event_store import EventStore
from costweave.security_store import ProjectRevokedError, SecurityStore


class AuroraFrozenTaskTests(unittest.TestCase):
    def test_task_is_frozen_and_oracle_is_not_model_visible(self) -> None:
        initial = public_task(reveal_incident=False)
        record = frozen_task_record()
        self.assertEqual(AURORA_TASK_VERSION, initial["task_version"])
        self.assertFalse(initial["oracle_included"])
        self.assertNotIn("incident", initial)
        self.assertIn("incident_commitment_sha256", initial)
        self.assertEqual(64, len(record["initial_task_sha256"]))
        self.assertEqual(sealed_oracle_digest(), record["oracle_commitment_sha256"])
        self.assertEqual(0, record["external_calls"])

    def test_dag_has_24_unique_nodes_and_valid_dependencies(self) -> None:
        node_ids = [node.node_id for node in NODES]
        self.assertEqual(24, len(node_ids))
        self.assertEqual(24, len(set(node_ids)))
        order = {node_id: index for index, node_id in enumerate(node_ids)}
        for node in NODES:
            self.assertTrue(all(dependency in order for dependency in node.dependencies))
            self.assertTrue(all(order[dependency] < order[node.node_id] for dependency in node.dependencies))
        self.assertEqual(8, len(STAGES))


class AuroraValidatorTests(unittest.TestCase):
    def test_sealed_oracle_passes_all_24_nodes(self) -> None:
        result = run_oracle_preflight()
        self.assertTrue(result["passed"])
        self.assertTrue(result["oracle_hidden"])
        self.assertEqual((24, 24), (result["accepted_nodes"], result["total_nodes"]))
        self.assertEqual(0, result["network_calls"])

    def test_allocator_validator_executes_cases_not_self_report(self) -> None:
        valid = oracle_artifact("allocator_patch")
        self.assertTrue(all(allocator_test_results(valid).values()))
        mutant = copy.deepcopy(valid)
        mutant["implementation_id"] = "round_each_share_v0"
        self.assertFalse(validate_node("allocator_patch", mutant).accepted)

    def test_tenant_validator_catches_cross_tenant_export(self) -> None:
        valid = oracle_artifact("tenant_policy_patch")
        self.assertTrue(all(tenant_test_results(valid).values()))
        mutant = copy.deepcopy(valid)
        mutant["same_tenant_required"] = False
        self.assertFalse(tenant_test_results(mutant)["case_4"])
        self.assertFalse(validate_node("tenant_policy_patch", mutant).accepted)

    def test_deployment_validator_catches_residency_and_capacity(self) -> None:
        mutant = oracle_artifact("deployment_plan")
        mutant["regional_routes"]["EU"] = {"A": 700}
        self.assertFalse(validate_node("deployment_plan", mutant).accepted)

    def test_media_validator_enforces_cost_and_claim_controls(self) -> None:
        mutant = oracle_artifact("media_package")
        mutant["shots"][3]["new_keyframe"] = True
        mutant["new_keyframe_count"] = 4
        self.assertFalse(validate_node("media_package", mutant).accepted)
        mutant = oracle_artifact("media_package")
        mutant["shots"][0]["prompt"] = "100% accurate output"
        self.assertFalse(validate_node("media_package", mutant).accepted)

    def test_final_assembly_cannot_override_failed_review(self) -> None:
        context = {node.node_id: oracle_artifact(node.node_id) for node in NODES}
        context["security_review"]["accepted"] = False
        result = validate_node(
            "final_assembly",
            oracle_artifact("final_assembly"),
            context,
            incident_revealed=True,
        )
        self.assertFalse(result.accepted)


class AuroraFaultAndReadinessTests(unittest.TestCase):
    def test_fault_matrix_is_fully_offline_and_passes(self) -> None:
        result = run_fault_matrix()
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["total_cases"], 24)
        self.assertEqual(result["total_cases"], result["passed_cases"])
        self.assertEqual(0, result["network_calls"])
        self.assertEqual(0.0, result["paid_cost_usd"])


class AuroraExecutionReadinessTests(unittest.TestCase):
    def test_waiting_revocation_blocks_late_session_and_denies_it_after_commit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session-gate.sqlite3"
            EventStore(path)
            first_store = SecurityStore(path)
            first_entered = threading.Event()
            release_first = threading.Event()
            revocation_done = threading.Event()
            late_entered = threading.Event()
            late_denied = threading.Event()

            def first_session() -> None:
                with first_store.active_project_session("project-session-gate"):
                    first_entered.set()
                    release_first.wait(3)

            def revoke() -> None:
                SecurityStore(path).revoke_project(
                    "project-session-gate",
                    idempotency_key="revoke-project-session-gate-001",
                )
                revocation_done.set()

            def late_session() -> None:
                try:
                    with SecurityStore(path).active_project_session(
                        "project-session-gate"
                    ):
                        late_entered.set()
                except ProjectRevokedError:
                    late_denied.set()

            first_thread = threading.Thread(target=first_session)
            first_thread.start()
            self.assertTrue(first_entered.wait(1))
            revoke_thread = threading.Thread(target=revoke)
            revoke_thread.start()
            time.sleep(0.05)
            late_thread = threading.Thread(target=late_session)
            late_thread.start()
            time.sleep(0.05)
            self.assertFalse(revocation_done.is_set())
            self.assertFalse(late_entered.is_set())
            release_first.set()
            first_thread.join(3)
            revoke_thread.join(3)
            late_thread.join(3)
            self.assertTrue(revocation_done.is_set())
            self.assertTrue(late_denied.is_set())
            self.assertFalse(late_entered.is_set())

    def test_budget_fallback_requires_capability_and_affordability(self) -> None:
        candidates = [
            FallbackCandidate("cheap-low", "low", 0.01, 1.0, 1.0),
            FallbackCandidate("strong", "high", 0.04, 1.0, 1.0),
        ]
        decision = decide_budgeted_fallback(
            "security_review",
            error_code="semantic_validation_failed",
            primary_cost_usd=0.01,
            node_budget_usd=0.08,
            candidates=candidates,
        )
        self.assertEqual("strong", decision["selected_slot"])
        denied = decide_budgeted_fallback(
            "security_review",
            error_code="secret_exposure_detected",
            primary_cost_usd=0,
            node_budget_usd=0.08,
            candidates=candidates,
        )
        self.assertEqual("hard_security_stop", denied["reason"])

    def test_adapter_fixture_gate_proves_same_project_overlap(self) -> None:
        result = run_adapter_concurrency_gate()
        self.assertEqual(2, result["fixture_transport_calls"])
        self.assertEqual(0, result["network_calls"])
        self.assertTrue(result["passed"])
        self.assertGreaterEqual(result["maximum_active_transports"], 2)
        self.assertIsNone(result["blocker"])

    def test_offline_suite_reaches_paid_experiment_readiness(self) -> None:
        result = run_offline_suite()
        self.assertTrue(result["functional_passed"])
        self.assertTrue(result["paid_execution_ready"])
        self.assertEqual(0, result["network_calls"])
        self.assertEqual(0.0, result["paid_cost_usd"])


if __name__ == "__main__":
    unittest.main()
