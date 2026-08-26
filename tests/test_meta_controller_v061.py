import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path

from costweave.intelligence.beliefs import SystemBeliefV1, TaskBeliefV1
from costweave.intelligence.common import NonNegativeEstimate, ProbabilityInterval
from costweave.intelligence.meta_controller import (
    BudgetAmountV1,
    MetaControllerConfigV1,
    MetaControllerEngine,
    PlannerMemberV1,
    PlanningBudgetV1,
    UtilityIntervalV1,
    select_adaptive_committee,
)
from costweave.intelligence.strategy import StrategyCandidateV1, StrategyNodeV1
from costweave.meta_controller_service import CONFIRM_TOKEN, MetaControllerServiceError, RationalMetaControllerService
from costweave.meta_controller_store import MetaControllerStore, MetaControllerStoreError


NOW = "2026-08-09T00:00:00Z"


def make_beliefs(*, risk="low", ood=0.02, slots=2, queue=0, telemetry_age=0):
    objective_hash = hashlib.sha256(b"v061-test-objective").hexdigest()
    task = TaskBeliefV1(
        case_id="v061-case",
        objective_hash=objective_hash,
        task_family_probabilities={"code": 1.0},
        required_modalities=(), required_tools=(), risk_level=risk,
        reversible=True, external_side_effects=False,
        decomposability=ProbabilityInterval(mean=.5, lower=.2, upper=.8),
        parallelizability=ProbabilityInterval(mean=.5, lower=.2, upper=.8),
        verification_difficulty=ProbabilityInterval(mean=.3, lower=.1, upper=.6),
        epistemic_uncertainty=.1, aleatoric_uncertainty=.1, ood_score=ood,
        hard_constraints={"side_effects": False}, evidence_ids=("task-evidence",), observed_at=NOW,
    )
    system = SystemBeliefV1(
        case_id="v061-case", local_queue_depth=queue, local_available_slots=slots,
        local_gpu_memory_available_mb=None, budget_remaining_usd=1.0,
        deadline_remaining_ms=30_000, provider_beliefs=(), telemetry_age_ms=telemetry_age,
        evidence_ids=("system-evidence",), observed_at=NOW,
    )
    return task, system


def make_candidate(*, success=(.8, .65, .95), cost=(.01, .02), latency=(100, 200), checks=None, mode="no_delegation", name="node"):
    objective_hash = hashlib.sha256(b"v061-test-objective").hexdigest()
    node = StrategyNodeV1(
        node_id=name, objective_hash=objective_hash, dependencies=(), executor_id="local",
        reasoning_tier="medium", tool_ids=(), parallel_group="", validator_id="validator",
        predicted_success=ProbabilityInterval(mean=success[0], lower=success[1], upper=success[2]),
        predicted_cost_usd=NonNegativeEstimate(mean=cost[0], p95=cost[1]),
        predicted_latency_ms=NonNegativeEstimate(mean=latency[0], p95=latency[1]),
    )
    return StrategyCandidateV1(
        case_id="v061-case", management_mode=mode, nodes=(node,),
        planning_cost_usd=NonNegativeEstimate(mean=0.001, p95=.002),
        planning_latency_ms=NonNegativeEstimate(mean=10, p95=20),
        predicted_success=node.predicted_success,
        predicted_total_cost_usd=NonNegativeEstimate(mean=cost[0], p95=cost[1]),
        predicted_total_latency_ms=node.predicted_latency_ms,
        hard_constraint_checks=checks or {"safe": True}, recovery_policy="none",
        evidence_ids=(f"{name}-evidence",), policy_revision="v061-test",
    )


def make_member(member_id="planner-a", *, info=(.2, .1, .3), cost=.001, latency=20, group="a", available=True):
    return PlannerMemberV1(
        member_id=member_id, role="critic", information_value=UtilityIntervalV1(mean=info[0], lower=info[1], upper=info[2]),
        planning_cost_usd=NonNegativeEstimate(mean=cost, p95=cost),
        planning_latency_ms=NonNegativeEstimate(mean=latency, p95=latency),
        failure_correlation_group=group, evidence_level="E0-simulated", evidence_age_ms=0,
        specialties=("code",), available=available,
    )


class MetaControllerContractTests(unittest.TestCase):
    def test_budget_amount_is_multidimensional_and_bounded(self):
        amount = BudgetAmountV1(money_usd=.2, input_tokens=10, planner_calls=1)
        self.assertTrue(amount.fits_within(BudgetAmountV1(money_usd=.2, input_tokens=10, planner_calls=1)))
        self.assertFalse(amount.fits_within(BudgetAmountV1(money_usd=.1, input_tokens=10, planner_calls=1)))
        self.assertEqual(1.2, amount.add(BudgetAmountV1(money_usd=1.0)).money_usd)
        with self.assertRaises(ValueError):
            BudgetAmountV1(money_usd=-.1)

    def test_committee_contract_contains_only_selected_members(self):
        committee = select_adaptive_committee(
            [make_member(), make_member("planner-b", info=(.001, .0, .002), cost=.01)],
            case_id="v061-case", expected_regret_reduction=UtilityIntervalV1(mean=.2, lower=.0, upper=.4),
            budget=BudgetAmountV1(money_usd=.01, planner_calls=1, wall_clock_ms=100), now=NOW,
        )
        self.assertEqual(("planner-a",), committee.member_ids)
        self.assertEqual({"planner-a"}, set(committee.marginal_information_values))
        self.assertEqual(committee.call_count, len(committee.member_ids))

    def test_fast_path_is_deterministic_and_auditable(self):
        task, system = make_beliefs()
        candidate = make_candidate(success=(.95, .9, .98), cost=(.001, .002), latency=(20, 40))
        engine = MetaControllerEngine()
        first, committee = engine.decide(project_id="local-default", task_belief=task, system_belief=system, candidates=(candidate,), now=NOW)
        second, _ = engine.decide(project_id="local-default", task_belief=task, system_belief=system, candidates=(candidate,), now=NOW)
        self.assertEqual("fast", first.action)
        self.assertEqual(first, second)
        self.assertEqual("v0.6.1", first.schema_version)
        self.assertEqual(0, first.budget.money_usd)
        self.assertIn("hard_constraints_satisfied", first.reason_codes)
        self.assertIsNotNone(committee)
        self.assertEqual(0, committee.call_count)

    def test_expert_path_is_mandatory_for_risk_ood_stale_or_capacity(self):
        candidate = make_candidate()
        for kwargs, reason in (
            ({"risk": "high"}, "requires_expert_risk"),
            ({"ood": .8}, "requires_expert_ood"),
            ({"telemetry_age": 90_000_000}, "requires_expert_stale_evidence"),
            ({"slots": 0}, "requires_expert_capacity"),
        ):
            task, system = make_beliefs(**kwargs)
            decision, _ = MetaControllerEngine().decide(project_id="local-default", task_belief=task, system_belief=system, candidates=(candidate,), now=NOW)
            self.assertEqual("expert", decision.action)
            self.assertIn(reason, decision.reason_codes)

    def test_probe_and_deliberate_are_selected_only_when_value_supports_them(self):
        task, system = make_beliefs()
        first = make_candidate(success=(.8, .5, .99), cost=(.01, .01), latency=(100, 100), name="a")
        second = make_candidate(success=(.79, .5, .98), cost=(.01, .01), latency=(100, 100), name="b")
        member = make_member(info=(.2, .1, .3), cost=.001)
        probe, committee = MetaControllerEngine().decide(project_id="local-default", task_belief=task, system_belief=system, candidates=(first, second), planner_members=(member,), available_budget=BudgetAmountV1(money_usd=1, planner_calls=2, wall_clock_ms=1_000, probe_calls=1), now=NOW)
        self.assertEqual("probe", probe.action)
        self.assertEqual(1, probe.budget.probe_calls)
        self.assertEqual(("planner-a",), committee.member_ids)
        deliberate_engine = MetaControllerEngine(MetaControllerConfigV1(minimum_probe_value=1.0))
        deliberate, committee = deliberate_engine.decide(project_id="local-default", task_belief=task, system_belief=system, candidates=(first, second), planner_members=(make_member(info=(.005, .0, .01), cost=.0001),), available_budget=BudgetAmountV1(money_usd=1, planner_calls=2, wall_clock_ms=1_000, probe_calls=1), now=NOW)
        self.assertEqual("deliberate", deliberate.action)
        self.assertEqual(1, committee.call_count)

    def test_no_feasible_candidate_never_silently_executes(self):
        task, system = make_beliefs()
        candidate = make_candidate(checks={"safe": False})
        decision, _ = MetaControllerEngine().decide(project_id="local-default", task_belief=task, system_belief=system, candidates=(candidate,), now=NOW)
        self.assertEqual("expert", decision.action)
        self.assertIsNone(decision.selected_strategy_id)
        self.assertIn("requires_expert_no_feasible_strategy", decision.reason_codes)

    def test_round_trip_keeps_decision_hash(self):
        task, system = make_beliefs()
        candidate = make_candidate()
        decision, _ = MetaControllerEngine().decide(project_id="local-default", task_belief=task, system_belief=system, candidates=(candidate,), now=NOW)
        self.assertEqual(decision, type(decision).from_dict(decision.to_dict()))


class MetaControllerPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.store = MetaControllerStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_decision_record_is_idempotent_and_project_scoped(self):
        task, system = make_beliefs()
        decision, _ = MetaControllerEngine().decide(project_id="local-default", task_belief=task, system_belief=system, candidates=(make_candidate(),), now=NOW)
        self.assertTrue(self.store.record_decision(decision))
        self.assertFalse(self.store.record_decision(decision))
        self.assertEqual(decision, self.store.decision(project_id="local-default", decision_id=decision.decision_id))
        self.assertIsNone(self.store.decision(project_id="other-project", decision_id=decision.decision_id))
        self.assertEqual(1, self.store.counts()["meta_decisions_v061"])

    def test_budget_ledger_reserve_release_commit_and_overrun(self):
        budget = PlanningBudgetV1(budget_id="budget-v061", project_id="local-default", limits=BudgetAmountV1(money_usd=1, planner_calls=3), created_at=NOW)
        self.assertTrue(self.store.create_budget(budget))
        self.assertFalse(self.store.create_budget(budget))
        amount = BudgetAmountV1(money_usd=.4, planner_calls=1)
        self.assertTrue(self.store.reserve(project_id="local-default", budget_id=budget.budget_id, reservation_id="reservation-1", amount=amount, recorded_at=NOW))
        self.assertFalse(self.store.reserve(project_id="local-default", budget_id=budget.budget_id, reservation_id="reservation-1", amount=amount, recorded_at=NOW))
        self.assertTrue(self.store.commit(project_id="local-default", budget_id=budget.budget_id, reservation_id="reservation-1", amount=amount, recorded_at=NOW))
        second = BudgetAmountV1(money_usd=.2, planner_calls=1)
        self.assertTrue(self.store.reserve(project_id="local-default", budget_id=budget.budget_id, reservation_id="reservation-2", amount=second, recorded_at=NOW))
        self.assertTrue(self.store.release(project_id="local-default", budget_id=budget.budget_id, reservation_id="reservation-2", amount=second, recorded_at=NOW))
        snapshot = self.store.budget_snapshot(project_id="local-default", budget_id=budget.budget_id)
        self.assertAlmostEqual(.4, snapshot["spent"]["money_usd"])
        self.assertAlmostEqual(.6, snapshot["available"]["money_usd"])
        with self.assertRaises(MetaControllerStoreError):
            self.store.reserve(project_id="local-default", budget_id=budget.budget_id, reservation_id="reservation-3", amount=BudgetAmountV1(money_usd=.7, planner_calls=1), recorded_at=NOW)

    def test_budget_and_decision_tables_are_immutable(self):
        budget = PlanningBudgetV1(budget_id="budget-v061", project_id="local-default", limits=BudgetAmountV1(money_usd=1), created_at=NOW)
        self.store.create_budget(budget)
        with self.assertRaises(sqlite3.IntegrityError):
            connection = sqlite3.connect(self.path)
            try:
                connection.execute("UPDATE planning_budget_accounts_v061 SET budget_id='changed' WHERE budget_id=?", (budget.budget_id,))
            finally:
                connection.close()


class MetaControllerServiceTests(unittest.TestCase):
    def test_service_requires_explicit_offline_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = RationalMetaControllerService(MetaControllerStore(Path(directory) / "runtime.sqlite3"))
            with self.assertRaises(MetaControllerServiceError):
                service.decide(project_id="local-default", idempotency_key="key-v061-1", payload={})

    def test_service_returns_replay_safe_result_without_provider_calls(self):
        from costweave.meta_controller_cli import _demo
        task, system, candidates = _demo()
        payload = {
            "confirm": CONFIRM_TOKEN,
            "task_belief": task.to_dict(), "system_belief": system.to_dict(),
            "candidates": [item.to_dict() for item in candidates],
            "available_budget": BudgetAmountV1(money_usd=1).to_dict(), "now": NOW,
        }
        with tempfile.TemporaryDirectory() as directory:
            service = RationalMetaControllerService(MetaControllerStore(Path(directory) / "runtime.sqlite3"))
            first, replayed = service.decide(project_id="local-default", idempotency_key="key-v061-1", payload=payload)
            second, replayed_again = service.decide(project_id="local-default", idempotency_key="key-v061-1", payload=payload)
            self.assertFalse(replayed)
            self.assertTrue(replayed_again)
            self.assertEqual(first["decision"]["decision_id"], second["decision"]["decision_id"])
            self.assertEqual(0, first["provider_calls"])
            self.assertEqual(0, first["network_calls"])


if __name__ == "__main__":
    unittest.main()
