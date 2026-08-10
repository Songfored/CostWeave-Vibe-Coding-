import json
import sqlite3
import tempfile
import unittest

from costweave.intelligence.joint_search import (
    JointSearchCandidateV1,
    JointSearchDecisionV1,
    JointStrategySearcher,
    PromptHandoffAuditV1,
    build_demo_problem,
    compile_prompt_handoff,
)
from costweave.joint_search_service import CONFIRM_TOKEN, JointSearchService, JointSearchServiceError
from costweave.joint_search_store import JointSearchStore, JointSearchStoreError


class JointSearchContractTests(unittest.TestCase):
    def test_joint_search_is_deterministic_and_selects_anytime_best(self):
        problem = build_demo_problem()
        first, candidates = JointStrategySearcher().search(problem, searcher="beam")
        second, _ = JointStrategySearcher().search(problem, searcher="beam")
        self.assertEqual("SELECT", first.action)
        self.assertEqual(first.selected_candidate_id, second.selected_candidate_id)
        self.assertEqual(first.trace_hash, second.trace_hash)
        self.assertGreater(first.evaluated_count, 0)
        self.assertEqual(first.feasible_count, first.evaluated_count)
        self.assertLessEqual(first.evaluated_count, problem.budget.max_evaluations)
        self.assertTrue(first.selected_candidate.compiler_audit.passed)
        self.assertEqual(first.selected_candidate_id, first.selected_candidate.computed_id)
        self.assertEqual(len(candidates), first.evaluated_count)

    def test_all_searchers_are_bounded_and_reproducible(self):
        problem = build_demo_problem()
        for searcher in ("greedy", "beam", "successive_halving", "pareto"):
            first, _ = JointStrategySearcher().search(problem, searcher=searcher)
            second, _ = JointStrategySearcher().search(problem, searcher=searcher)
            self.assertEqual(first.action, "SELECT")
            self.assertEqual(first.selected_candidate_id, second.selected_candidate_id)
            self.assertLessEqual(first.evaluated_count, problem.budget.max_evaluations)

    def test_compiler_detects_shared_contradiction_and_authority_gap(self):
        problem = build_demo_problem()
        choices = {item.node_id: item.options[0] for item in problem.route_sets}
        choices["verify"] = type(choices["verify"]).from_dict(
            {**choices["verify"].to_dict(), "handoff_fields": {"shared:contract": "different"}}
        )
        choices["inspect"] = type(choices["inspect"]).from_dict(
            {**choices["inspect"].to_dict(), "handoff_fields": {"shared:contract": "one"}}
        )
        audit = compile_prompt_handoff(problem, choices)
        self.assertFalse(audit.passed)
        self.assertIn("handoff_contradiction", audit.reason_codes)

    def test_hard_budget_and_chance_constraints_fail_closed(self):
        problem = build_demo_problem()
        constrained = type(problem).from_dict({**problem.to_dict(), "min_success_lower": 0.999})
        decision, _ = JointStrategySearcher().search(constrained)
        self.assertIn(decision.action, {"ABSTAIN", "ESCALATE"})
        self.assertIsNone(decision.selected_candidate)

    def test_candidate_and_decision_round_trip(self):
        decision, _ = JointStrategySearcher().search(build_demo_problem())
        candidate = JointSearchCandidateV1.from_dict(decision.selected_candidate.to_dict())
        restored = JointSearchDecisionV1.from_dict(decision.to_dict())
        self.assertEqual(candidate.computed_id, decision.selected_candidate_id)
        self.assertEqual(restored.to_dict(), decision.to_dict())
        self.assertEqual(PromptHandoffAuditV1.from_dict(candidate.compiler_audit.to_dict()), candidate.compiler_audit)


class JointSearchPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = JointSearchStore(f"{self.temp.name}/runtime.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_and_immutable_idempotent_records(self):
        self.assertEqual("v6.3.0", self.store._runtime.health()["runtime_schema"])
        self.assertEqual(33, self.store._runtime.health()["migrations"])
        payload = {"confirm": CONFIRM_TOKEN, "problem": build_demo_problem().to_dict(), "searcher": "beam"}
        service = JointSearchService(self.store)
        result, replayed = service.decide(project_id="local-default", idempotency_key="joint-persist-1", payload=payload)
        self.assertFalse(replayed)
        again, replayed_again = service.decide(project_id="local-default", idempotency_key="joint-persist-1", payload=payload)
        self.assertTrue(replayed_again)
        self.assertEqual(result["decision"]["decision_id"], again["decision"]["decision_id"])
        self.assertEqual(16, self.store.counts()["joint_search_candidates_v063"])
        with self.assertRaises(sqlite3.IntegrityError):
            connection = sqlite3.connect(self.store.path)
            try:
                connection.execute("DELETE FROM joint_search_decisions_v063")
            finally:
                connection.close()

    def test_project_scoping_and_idempotency_conflict(self):
        payload = {"confirm": CONFIRM_TOKEN, "problem": build_demo_problem().to_dict(), "searcher": "greedy"}
        service = JointSearchService(self.store)
        service.decide(project_id="local-default", idempotency_key="joint-project-1", payload=payload)
        with self.assertRaises(JointSearchServiceError):
            service.decide(project_id="other-project", idempotency_key="joint-project-1", payload=payload)
        changed = {**payload, "searcher": "pareto"}
        with self.assertRaises(JointSearchStoreError):
            service.decide(project_id="local-default", idempotency_key="joint-project-1", payload=changed)

    def test_confirmation_and_no_provider_calls(self):
        payload = {"problem": build_demo_problem().to_dict(), "searcher": "beam"}
        with self.assertRaises(JointSearchServiceError):
            JointSearchService(self.store).decide(project_id="local-default", idempotency_key="joint-confirm-1", payload=payload)
        result, _ = JointSearchService(self.store).decide(project_id="local-default", idempotency_key="joint-confirm-2", payload={**payload, "confirm": CONFIRM_TOKEN})
        self.assertEqual(0, result["provider_calls"])
        self.assertEqual(0, result["network_calls"])
        self.assertFalse(result["commercial_claims_allowed"])


if __name__ == "__main__":
    unittest.main()
