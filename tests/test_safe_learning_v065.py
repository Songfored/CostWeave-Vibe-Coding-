import sqlite3
import tempfile
import unittest
from dataclasses import replace

from costweave.intelligence.common import CREDIT_COMPONENTS
from costweave.intelligence.safe_learning import (
    SAFE_LEARNING_CONFIRM_TOKEN,
    CounterfactualReplayV1,
    CreditAssignmentV1,
    LearningEstimateV1,
    LoggedDecisionV1,
    PromotionAssessmentV1,
    SafeLearningCandidateV1,
    TraceCreditInputV1,
    assign_credit,
    estimate_policy,
    run_demo,
)
from costweave.safe_learning_service import SafeLearningService, SafeLearningServiceError
from costweave.safe_learning_store import SafeLearningStore, SafeLearningStoreError


def trace(*, evidence="E0-simulated", origin="test_fixture", independent=False):
    return TraceCreditInputV1(
        project_id="local-default", trace_id="trace-065",
        accepted=True, reward=0.72, total_cost_usd=0.01, total_latency_ms=1200,
        component_effects={component: 0.1 for component in CREDIT_COMPONENTS},
        component_confidence={component: 0.8 for component in CREDIT_COMPONENTS},
        evidence_level=evidence, data_origin=origin,
        independently_validated=independent, evidence_ids=("trace-evidence",),
        created_at="2026-01-01T00:00:00+00:00",
    )


def observations(*, evidence="E0-simulated", origin="test_fixture", independent=False):
    return tuple(LoggedDecisionV1(
        observation_id=f"observation-{index:02d}", project_id="local-default",
        task_family="classification", risk_level="low", logged_action="terra",
        logging_action_probabilities={"luna": 0.25, "terra": 0.75},
        candidate_action_probabilities={"luna": 0.4, "terra": 0.6},
        predicted_rewards={"luna": 0.55, "terra": 0.75},
        observed_reward=0.70 + (index % 3) * 0.02,
        evidence_level=evidence, data_origin=origin,
        independently_validated=independent,
        created_at="2026-01-01T00:00:00+00:00",
    ) for index in range(12))


class SafeLearningContractTests(unittest.TestCase):
    def test_trace_credit_round_trip_and_e0_is_not_learning(self):
        credit = assign_credit(trace())
        restored = CreditAssignmentV1.from_dict(credit.to_dict())
        self.assertEqual(credit.to_dict(), restored.to_dict())
        self.assertFalse(credit.usable_for_learning)
        self.assertTrue(credit.credit_id.startswith("credit065_"))

    def test_e2_real_independent_credit_is_eligible(self):
        credit = assign_credit(trace(evidence="E2-directional-real", origin="production", independent=True))
        self.assertTrue(credit.usable_for_learning)

    def test_counterfactual_replay_delta_is_deterministic(self):
        replay = CounterfactualReplayV1.replay(trace(), "routing", 0.35, assumptions=("isolated",))
        self.assertEqual(replay.delta, replay.counterfactual_reward - replay.baseline_reward)
        self.assertTrue(replay.deterministic)
        self.assertTrue(replay.replay_id.startswith("replay065_"))

    def test_observation_rejects_missing_propensity_and_bad_distribution(self):
        with self.assertRaises(ValueError):
            LoggedDecisionV1(
                observation_id="bad-observation", project_id="local-default",
                task_family="classification", risk_level="low", logged_action="terra",
                logging_action_probabilities={"terra": 0.0},
                candidate_action_probabilities={"terra": 1.0},
                predicted_rewards={"terra": 0.5}, observed_reward=0.5,
                evidence_level="E2-directional-real", data_origin="production",
                independently_validated=True, created_at="2026-01-01T00:00:00+00:00",
            )

    def test_ips_and_dr_have_traceable_observation_ids(self):
        ips = estimate_policy(observations(), policy_id="p", policy_version="1", task_family="classification", method="ips")
        dr = estimate_policy(observations(), policy_id="p", policy_version="1", task_family="classification", method="dr")
        self.assertEqual(12, ips.sample_count)
        self.assertEqual(12, len(dr.observation_ids))
        self.assertFalse(ips.usable_for_learning)
        self.assertIn("evidence_below_learning_floor", ips.reason_codes)

    def test_real_e2_estimate_can_pass_learning_gates(self):
        estimate = estimate_policy(
            observations(evidence="E2-directional-real", origin="production", independent=True),
            policy_id="p", policy_version="1", task_family="classification",
        )
        self.assertTrue(estimate.usable_for_learning)
        self.assertTrue(estimate.common_support)
        self.assertGreaterEqual(estimate.effective_sample_size, 8)

    def test_candidate_and_assessment_fail_closed_for_e0_demo(self):
        estimate = estimate_policy(observations(), policy_id="p", policy_version="1", task_family="classification")
        candidate = SafeLearningCandidateV1.create(
            project_id="local-default", candidate_policy_id="p", candidate_policy_version="1",
            champion_policy_id="champion", champion_policy_version="1",
            task_family="classification", estimate=estimate, champion_estimate=0.68,
            rollback_policy_id="rollback",
        )
        assessment = PromotionAssessmentV1.assess_candidate(candidate, estimate)
        self.assertEqual("rejected", assessment.state)
        self.assertIn("estimate_not_usable_for_learning", assessment.reason_codes)
        self.assertFalse(assessment.route_mutation_allowed)

    def test_demo_reports_no_provider_calls_and_rejected_candidate(self):
        payload = run_demo()
        self.assertEqual("v0.6.5", payload["schema_version"])
        self.assertEqual("rejected", payload["assessment"]["state"])
        self.assertFalse(payload["training_enabled"])
        self.assertEqual(0, payload["provider_calls"])
        self.assertEqual(0, payload["network_calls"])


class SafeLearningPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = SafeLearningStore(f"{self.temp.name}/runtime.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def test_immutable_credit_idempotency_and_project_scope(self):
        service = SafeLearningService(self.store)
        item = trace()
        payload = {"confirm": SAFE_LEARNING_CONFIRM_TOKEN, "trace": item.to_dict()}
        result, replayed = service.credit(project_id="local-default", idempotency_key="safe-credit-001", payload=payload)
        self.assertFalse(replayed)
        again, replayed_again = service.credit(project_id="local-default", idempotency_key="safe-credit-001", payload=payload)
        self.assertTrue(replayed_again)
        self.assertEqual(result["credit"]["credit_id"], again["credit"]["credit_id"])
        self.assertEqual(1, self.store.counts()["safe_learning_credits_v065"])
        with self.assertRaises(sqlite3.IntegrityError):
            connection = sqlite3.connect(self.store.path)
            try:
                connection.execute("DELETE FROM safe_learning_credits_v065")
            finally:
                connection.close()
        with self.assertRaises(SafeLearningServiceError):
            service.credit(project_id="other-project", idempotency_key="safe-credit-002", payload=payload)

    def test_evaluate_persists_estimate_candidate_and_assessment(self):
        service = SafeLearningService(self.store)
        payload = {
            "confirm": SAFE_LEARNING_CONFIRM_TOKEN, "policy_id": "p", "policy_version": "1",
            "candidate_policy_id": "p", "candidate_policy_version": "1",
            "champion_policy_id": "champion", "champion_policy_version": "1",
            "champion_estimate": 0.68, "rollback_policy_id": "rollback",
            "task_family": "classification", "method": "dr", "stage": "shadow",
            "observations": [item.to_dict() for item in observations()],
        }
        result, replayed = service.evaluate(project_id="local-default", idempotency_key="safe-eval-001", payload=payload)
        self.assertFalse(replayed)
        self.assertEqual("rejected", result["assessment"]["state"])
        self.assertEqual(1, self.store.counts()["safe_learning_estimates_v065"])
        self.assertEqual(1, self.store.counts()["safe_learning_candidates_v065"])
        self.assertEqual(1, self.store.counts()["safe_learning_assessments_v065"])
        again, replayed_again = service.evaluate(project_id="local-default", idempotency_key="safe-eval-001", payload=payload)
        self.assertTrue(replayed_again)
        self.assertEqual(result["assessment"]["assessment_id"], again["assessment"]["assessment_id"])

    def test_confirmation_and_unknown_fields_fail_closed(self):
        service = SafeLearningService(self.store)
        with self.assertRaises(SafeLearningServiceError):
            service.credit(project_id="local-default", idempotency_key="safe-credit-003", payload={"trace": trace().to_dict()})
        with self.assertRaises(SafeLearningServiceError):
            service.credit(project_id="local-default", idempotency_key="safe-credit-004", payload={"confirm": SAFE_LEARNING_CONFIRM_TOKEN, "trace": trace().to_dict(), "unknown": 1})


if __name__ == "__main__":
    unittest.main()
