import sqlite3
import tempfile
import unittest
from dataclasses import replace

from costweave.execution_recovery_service import (
    CONFIRM_TOKEN,
    ExecutionRecoveryService,
    ExecutionRecoveryServiceError,
)
from costweave.execution_recovery_store import ExecutionRecoveryStore, ExecutionRecoveryStoreError
from costweave.intelligence.execution_recovery import (
    EXECUTION_ACTIONS,
    EconomicRecoveryPlanner,
    ExecutionBeliefUpdater,
    ExecutionNodeStateV1,
    ExecutionRecoveryDecisionV1,
    ExecutionResourceBeliefV1,
    ExecutionSnapshotV1,
    build_demo_snapshot,
    run_demo,
)


class ExecutionRecoveryContractTests(unittest.TestCase):
    def test_fault_injection_chooses_dynamic_switch_with_cps_advantage(self):
        decision = run_demo()
        self.assertEqual("SWITCH_MODEL", decision.action)
        self.assertGreater(decision.recovery_advantage_usd, 0.0)
        self.assertEqual(set(EXECUTION_ACTIONS), {item.action for item in decision.options})
        self.assertIn("draft", decision.belief.pollution_scope)
        self.assertIn("polish", decision.belief.pollution_scope)
        self.assertGreater(decision.belief.reusable_artifact_savings_usd, 0.0)

    def test_belief_round_trip_excludes_observation_time_from_id(self):
        snapshot = build_demo_snapshot()
        belief = ExecutionBeliefUpdater().update(snapshot)
        restored = type(belief).from_dict(belief.to_dict())
        self.assertEqual(belief.to_dict(), restored.to_dict())
        shifted = replace(snapshot, observed_at="2026-08-10T01:00:00Z")
        shifted_belief = ExecutionBeliefUpdater().update(shifted)
        self.assertEqual(belief.belief_id, shifted_belief.belief_id)
        self.assertEqual(snapshot.spent_cost_usd, belief.sunk_cost_excluded_usd)

    def test_dynamic_resource_shock_makes_continue_infeasible(self):
        snapshot = build_demo_snapshot()
        resource = replace(snapshot.resource, queue_depth=100, available_slots=0, price_multiplier=2.5)
        shocked = replace(snapshot, resource=resource, trigger_kinds=("resource_change", "price_change"))
        decision, _ = EconomicRecoveryPlanner().decide(shocked)
        continue_option = next(item for item in decision.options if item.action == "CONTINUE")
        self.assertFalse(continue_option.feasible)
        self.assertIn("dynamic_resource_or_deadline", continue_option.hard_checks)
        self.assertNotEqual("CONTINUE", decision.action)

    def test_budget_and_validator_constraints_fail_closed(self):
        snapshot = build_demo_snapshot()
        resource = replace(snapshot.resource, alternate_route_available=False, orthogonal_validator_available=False)
        constrained = replace(snapshot, resource=resource, budget_remaining_usd=0.0, deadline_remaining_ms=1)
        decision, _ = EconomicRecoveryPlanner().decide(constrained)
        self.assertEqual("STOP_AND_RETURN_PARTIAL", decision.action)
        for option in decision.options:
            if option.action in {"SWITCH_MODEL", "REPLAN_SUBGRAPH", "FULL_REPLAN"}:
                self.assertFalse(option.feasible)

    def test_cycle_in_execution_dag_is_rejected(self):
        snapshot = build_demo_snapshot()
        nodes = list(snapshot.nodes)
        first = nodes[0]
        nodes[0] = replace(first, dependencies=("final",), dependency_types={"final": "hard"})
        with self.assertRaises(ValueError):
            ExecutionBeliefUpdater().update(replace(snapshot, nodes=tuple(nodes)))

    def test_decision_round_trip(self):
        decision = run_demo()
        restored = ExecutionRecoveryDecisionV1.from_dict(decision.to_dict())
        self.assertEqual(decision.to_dict(), restored.to_dict())
        self.assertEqual(decision.computed_id, restored.computed_id)


class ExecutionRecoveryPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = ExecutionRecoveryStore(f"{self.temp.name}/runtime.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_and_immutable_idempotent_records(self):
        self.assertEqual("v6.5.0", self.store._runtime.health()["runtime_schema"])
        self.assertEqual(35, self.store._runtime.health()["migrations"])
        payload = {"confirm": CONFIRM_TOKEN, "snapshot": build_demo_snapshot().to_dict()}
        service = ExecutionRecoveryService(self.store)
        result, replayed = service.decide(project_id="local-default", idempotency_key="recovery-persist-1", payload=payload)
        self.assertFalse(replayed)
        again, replayed_again = service.decide(project_id="local-default", idempotency_key="recovery-persist-1", payload=payload)
        self.assertTrue(replayed_again)
        self.assertEqual(result["decision"]["decision_id"], again["decision"]["decision_id"])
        self.assertEqual(1, self.store.counts()["execution_beliefs_v064"])
        self.assertEqual(1, self.store.counts()["execution_recovery_decisions_v064"])
        with self.assertRaises(sqlite3.IntegrityError):
            connection = sqlite3.connect(self.store.path)
            try:
                connection.execute("DELETE FROM execution_recovery_decisions_v064")
            finally:
                connection.close()

    def test_project_scope_and_idempotency_conflict(self):
        payload = {"confirm": CONFIRM_TOKEN, "snapshot": build_demo_snapshot().to_dict()}
        service = ExecutionRecoveryService(self.store)
        service.decide(project_id="local-default", idempotency_key="recovery-project-1", payload=payload)
        with self.assertRaises(ExecutionRecoveryServiceError):
            service.decide(project_id="other-project", idempotency_key="recovery-project-1", payload=payload)
        changed = {**payload, "snapshot": {**payload["snapshot"], "case_id": "changed-case"}}
        with self.assertRaises(ExecutionRecoveryStoreError):
            service.decide(project_id="local-default", idempotency_key="recovery-project-1", payload=changed)

    def test_confirmation_and_offline_boundary(self):
        payload = {"snapshot": build_demo_snapshot().to_dict()}
        with self.assertRaises(ExecutionRecoveryServiceError):
            ExecutionRecoveryService(self.store).decide(project_id="local-default", idempotency_key="recovery-confirm-1", payload=payload)
        result, _ = ExecutionRecoveryService(self.store).decide(
            project_id="local-default", idempotency_key="recovery-confirm-2", payload={**payload, "confirm": CONFIRM_TOKEN}
        )
        self.assertEqual(0, result["provider_calls"])
        self.assertEqual(0, result["network_calls"])
        self.assertTrue(result["advisory_only"])


if __name__ == "__main__":
    unittest.main()
