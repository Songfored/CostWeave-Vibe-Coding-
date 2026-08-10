from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import replace

from costweave.event_store import EventStore
from costweave.policy_candidate import (
    LoggedBanditObservation,
    OfflineBanditEvaluator,
    OfflineBanditPolicy,
    PolicyCandidate,
    PolicyCandidateError,
    PolicySafetySignals,
    assess_exploration,
    assess_policy_freeze,
    build_policy_card,
    public_policy_candidate_policy,
)
from costweave.policy_candidate_store import PolicyCandidateStore
from costweave.predictor_evaluation import PredictorEvaluationGate
from tests.test_predictor_evaluation_v05642 import (
    COHORT_HASH,
    admitted_report,
    artifact,
    observations,
)


def passing_evaluation(*, evidence="E3-independent-real"):
    return PredictorEvaluationGate().evaluate(
        artifact(),
        admitted_report(),
        observations(evidence=evidence),
        cohort_hash=COHORT_HASH,
    )


def candidate(*, evidence="E3-independent-real"):
    return PolicyCandidate.create(
        artifact(),
        passing_evaluation(evidence=evidence),
        objective_revision="expected-total-loss-v1",
        constraint_revision="portfolio-constraints-v1",
        allowed_task_families=("code", "research"),
        prohibited_task_families=("security-critical",),
        data_window_start="2026-07-01T00:00:00Z",
        data_window_end="2026-07-31T23:59:59Z",
        training_report_id="training-report-v1",
        calibration_report_id="calibration-report-v1",
        test_report_id="test-report-v1",
        rollback_policy_id="legacy-resource-optimizer-v1",
        known_strengths=("code-low-risk",),
        failure_modes=("out-of-distribution",),
        unsupported_scopes=("irreversible-production-write",),
    )


def bandit_observations(
    *,
    count=40,
    evidence="E3-independent-real",
    origin="human_benchmark",
    task_family="code",
):
    result = []
    for index in range(count):
        logged_action = "cheap" if index % 2 == 0 else "strong"
        result.append(
            LoggedBanditObservation(
                observation_id=f"banditobs_{index:032x}",
                anonymous_context_id=f"context_{index:032x}",
                task_family=task_family,
                risk_level="low",
                reversible=True,
                permission_sensitive=False,
                evidence_level=evidence,
                data_origin=origin,
                logged_action=logged_action,
                logging_action_probabilities={"cheap": 0.5, "strong": 0.5},
                candidate_action_probabilities={"cheap": 0.25, "strong": 0.75},
                predicted_rewards={"cheap": 0.4, "strong": 0.8},
                observed_reward=0.4 if logged_action == "cheap" else 0.8,
            )
        )
    return result


def clear_signals():
    return PolicySafetySignals(
        dataset_psi=0.10,
        ece_degradation=0.01,
        family_success_rate_deltas={"code": -0.01},
        cost_p95_degradation_windows=(0.10, 0.21, 0.10),
        latency_p95_degradation_windows=(0.05, 0.10, 0.15),
        data_quality_passed=True,
        privacy_check_passed=True,
    )


class PolicyCandidateContractTests(unittest.TestCase):
    def test_candidate_binds_predictor_data_reports_and_rollback(self):
        item = candidate()
        self.assertEqual("offline_candidate", item.status)
        self.assertEqual("legacy-resource-optimizer-v1", item.rollback_policy_id)
        self.assertFalse(item.routing_authority)
        self.assertFalse(item.automatic_promotion_allowed)
        self.assertEqual(item.candidate_id, candidate().candidate_id)

    def test_failed_or_e0_evaluation_cannot_create_candidate(self):
        failed = PredictorEvaluationGate().evaluate(
            artifact(),
            admitted_report(),
            observations(evidence="E0-fixture"),
            cohort_hash=COHORT_HASH,
        )
        with self.assertRaises(PolicyCandidateError):
            PolicyCandidate.create(
                artifact(),
                failed,
                objective_revision="loss-v1",
                constraint_revision="constraints-v1",
                allowed_task_families=("code",),
                prohibited_task_families=(),
                data_window_start="2026-07-01T00:00:00Z",
                data_window_end="2026-07-02T00:00:00Z",
                training_report_id="train",
                calibration_report_id="calibration",
                test_report_id="test",
                rollback_policy_id="legacy",
                known_strengths=(),
                failure_modes=(),
                unsupported_scopes=(),
            )

    def test_candidate_rejects_overlapping_scope_and_authority(self):
        item = candidate()
        with self.assertRaises(PolicyCandidateError):
            replace(item, default_policy=True)
        with self.assertRaises(PolicyCandidateError):
            replace(
                item,
                prohibited_task_families=item.allowed_task_families,
            )


class ExplorationGuardTests(unittest.TestCase):
    def test_low_risk_exploration_is_capped_at_five_percent(self):
        result = assess_exploration(
            risk_level="low",
            reversible=True,
            permission_sensitive=False,
            requested_rate=0.25,
        )
        self.assertEqual(0.05, result.approved_rate)
        self.assertTrue(result.exploration_allowed)
        self.assertIn("exploration_rate_capped", result.reasons)

    def test_high_risk_irreversible_and_sensitive_exploration_is_zero(self):
        result = assess_exploration(
            risk_level="high",
            reversible=False,
            permission_sensitive=True,
            requested_rate=0.05,
        )
        self.assertEqual(0.0, result.approved_rate)
        self.assertFalse(result.exploration_allowed)
        self.assertEqual(3, len(result.reasons))

    def test_probabilistic_high_risk_bandit_observation_is_rejected(self):
        with self.assertRaises(PolicyCandidateError):
            replace(bandit_observations(count=1)[0], risk_level="critical")


class OfflineBanditEvaluationTests(unittest.TestCase):
    def test_ips_snips_and_dr_match_known_policy_value(self):
        report = OfflineBanditEvaluator().evaluate(
            candidate(), bandit_observations()
        )
        self.assertEqual(0.7, report.estimates["ips_policy_value"])
        self.assertEqual(0.7, report.estimates["snips_policy_value"])
        self.assertEqual(0.7, report.estimates["doubly_robust_policy_value"])
        self.assertEqual(0.1, report.estimates["doubly_robust_delta"])
        self.assertTrue(report.common_support)
        self.assertTrue(report.counterfactual_conclusion_allowed)
        self.assertTrue(report.eligible_for_v057_shadow_review)
        self.assertEqual("shadow_review_candidate", report.state)
        self.assertFalse(report.routing_authority)

    def test_e2_is_directional_but_not_shadow_review_eligible(self):
        report = OfflineBanditEvaluator().evaluate(
            candidate(evidence="E2-real-validated"),
            bandit_observations(evidence="E2-real-validated"),
        )
        self.assertEqual("offline_directional", report.state)
        self.assertTrue(report.counterfactual_conclusion_allowed)
        self.assertFalse(report.eligible_for_v057_shadow_review)

    def test_fixture_origin_and_e0_never_allow_a_conclusion(self):
        report = OfflineBanditEvaluator().evaluate(
            candidate(),
            bandit_observations(evidence="E0-fixture", origin="test_fixture"),
        )
        self.assertEqual("blocked_evidence", report.state)
        self.assertFalse(report.counterfactual_conclusion_allowed)
        self.assertIn("real_origin_only", report.freeze_reasons)
        self.assertIn("e2_or_higher", report.freeze_reasons)

    def test_missing_common_support_fails_closed(self):
        items = [
            replace(
                item,
                logged_action="cheap",
                logging_action_probabilities={"cheap": 1.0, "strong": 0.0},
                candidate_action_probabilities={"cheap": 0.5, "strong": 0.5},
                observed_reward=0.4,
            )
            for item in bandit_observations()
        ]
        report = OfflineBanditEvaluator().evaluate(candidate(), items)
        self.assertEqual("blocked_support", report.state)
        self.assertFalse(report.common_support)
        self.assertFalse(report.counterfactual_conclusion_allowed)
        self.assertGreater(
            report.diagnostics["unsupported_action_context_pairs"], 0
        )

    def test_extreme_importance_weight_is_blocked(self):
        items = [
            replace(
                item,
                logged_action="strong",
                logging_action_probabilities={"cheap": 0.99, "strong": 0.01},
                candidate_action_probabilities={"cheap": 0.0, "strong": 1.0},
                observed_reward=0.8,
            )
            for item in bandit_observations()
        ]
        report = OfflineBanditEvaluator().evaluate(candidate(), items)
        self.assertTrue(report.common_support)
        self.assertEqual("blocked_support", report.state)
        self.assertFalse(report.checks["importance_weight_bounded"])

    def test_small_effective_sample_never_claims_a_result(self):
        report = OfflineBanditEvaluator().evaluate(
            candidate(), bandit_observations(count=10)
        )
        self.assertEqual("insufficient_effective_sample", report.state)
        self.assertFalse(report.counterfactual_conclusion_allowed)

    def test_out_of_scope_task_family_is_blocked(self):
        report = OfflineBanditEvaluator().evaluate(
            candidate(), bandit_observations(task_family="finance")
        )
        self.assertEqual("blocked_scope", report.state)
        self.assertFalse(report.checks["candidate_scope"])

    def test_bandit_evaluation_is_order_independent_and_deterministic(self):
        evaluator = OfflineBanditEvaluator()
        items = bandit_observations()
        first = evaluator.evaluate(candidate(), items)
        second = evaluator.evaluate(candidate(), tuple(reversed(items)))
        self.assertEqual(first.evaluation_id, second.evaluation_id)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_public_report_rejects_missing_or_contradictory_gates(self):
        report = OfflineBanditEvaluator().evaluate(
            candidate(), bandit_observations()
        )
        with self.assertRaises(PolicyCandidateError):
            replace(report, checks={})
        with self.assertRaises(PolicyCandidateError):
            replace(report, state="blocked_support")


class PolicyFreezeAndCardTests(unittest.TestCase):
    def test_all_documented_freeze_conditions_are_enforced(self):
        signals = PolicySafetySignals(
            dataset_psi=0.21,
            ece_degradation=0.031,
            family_success_rate_deltas={"code": -0.051},
            cost_p95_degradation_windows=(0.21, 0.22, 0.23),
            latency_p95_degradation_windows=(0.21, 0.22, 0.23),
            data_quality_passed=False,
            privacy_check_passed=False,
        )
        report = assess_policy_freeze(candidate(), signals)
        self.assertTrue(report.frozen)
        self.assertEqual("frozen", report.state)
        self.assertEqual(7, len(report.triggers))
        self.assertFalse(report.automatic_promotion_allowed)

    def test_threshold_equality_does_not_invent_a_freeze(self):
        signals = PolicySafetySignals(
            dataset_psi=0.20,
            ece_degradation=0.03,
            family_success_rate_deltas={"code": -0.05},
            cost_p95_degradation_windows=(0.20, 0.20, 0.20),
            latency_p95_degradation_windows=(0.20, 0.20, 0.20),
            data_quality_passed=True,
            privacy_check_passed=True,
        )
        report = assess_policy_freeze(candidate(), signals)
        self.assertFalse(report.frozen)
        self.assertEqual("clear", report.state)

    def test_policy_card_exposes_coverage_interval_and_rollback(self):
        item = candidate()
        evaluation = OfflineBanditEvaluator().evaluate(
            item, bandit_observations()
        )
        freeze = assess_policy_freeze(item, clear_signals())
        card = build_policy_card(item, evaluation, freeze)
        self.assertEqual("offline_review", card.status)
        self.assertTrue(card.eligible_for_v057_review)
        self.assertIsNotNone(card.expected_reward_delta_interval_95)
        self.assertEqual(item.rollback_policy_id, card.rollback_policy_id)
        self.assertFalse(card.commercial_claims_allowed)

    def test_freeze_blocks_policy_card_review(self):
        item = candidate()
        evaluation = OfflineBanditEvaluator().evaluate(
            item, bandit_observations()
        )
        freeze = assess_policy_freeze(
            item, replace(clear_signals(), privacy_check_passed=False)
        )
        card = build_policy_card(item, evaluation, freeze)
        self.assertEqual("frozen", card.status)
        self.assertFalse(card.eligible_for_v057_review)

    def test_public_policy_discloses_zero_authority_and_support_gate(self):
        policy = public_policy_candidate_policy()
        self.assertEqual("v0.5.6.5", policy["schema_version"])
        self.assertTrue(policy["common_support_required"])
        self.assertEqual(0.0, policy["high_risk_exploration_rate"])
        self.assertFalse(policy["automatic_promotion_enabled"])
        self.assertFalse(policy["fixture_metrics_are_claimable"])
        self.assertFalse(policy["external_calls_required"])


class PolicyCandidatePersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        self.health = EventStore(self.path).health()
        self.store = PolicyCandidateStore(self.path)
        self.candidate = candidate()
        self.evaluation = OfflineBanditEvaluator().evaluate(
            self.candidate, bandit_observations()
        )
        self.freeze = assess_policy_freeze(self.candidate, clear_signals())
        self.card = build_policy_card(
            self.candidate, self.evaluation, self.freeze
        )

    def tearDown(self):
        self.temp.cleanup()

    def persist_predictor_fixture(self):
        connection = sqlite3.connect(self.path)
        try:
            connection.execute(
                "INSERT INTO predictor_candidate_artifacts("
                "artifact_id,admission_report_id,dataset_content_hash,method,"
                "status,routing_authority,policy_promotion_allowed,"
                "artifact_json,artifact_hash,model_payload_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.candidate.predictor_artifact_id,
                    "admission_e3_test_fixture",
                    self.candidate.dataset_content_hash,
                    "hierarchical_beta",
                    "frozen_candidate",
                    0,
                    0,
                    "{}",
                    "a" * 64,
                    "1" * 64,
                    "2026-08-03T00:00:00Z",
                ),
            )
            connection.execute(
                "INSERT INTO predictor_evaluation_reports("
                "report_id,predictor_artifact_id,admission_report_id,cohort_hash,"
                "state,offline_evaluation_passed,eligible_for_shadow,"
                "routing_authority,policy_promotion_allowed,report_json,"
                "report_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.candidate.predictor_evaluation_report_id,
                    self.candidate.predictor_artifact_id,
                    "admission_e3_test_fixture",
                    COHORT_HASH,
                    "shadow_candidate",
                    1,
                    1,
                    0,
                    0,
                    "{}",
                    "b" * 64,
                    "2026-08-03T00:00:00Z",
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def test_migration_and_empty_registry(self):
        self.assertEqual("v6.4.0", self.health["runtime_schema"])
        self.assertEqual(34, self.health["migrations"])
        counts = self.store.counts()
        self.assertEqual(0, counts["policy_candidates"])
        self.assertEqual(0, counts["policy_production_promotions"])

    def test_records_are_linked_idempotent_exportable_and_zero_authority(self):
        self.persist_predictor_fixture()
        self.assertTrue(self.store.record_candidate(self.candidate))
        self.assertFalse(self.store.record_candidate(self.candidate))
        self.assertTrue(self.store.record_evaluation(self.evaluation))
        self.assertFalse(self.store.record_evaluation(self.evaluation))
        self.assertTrue(self.store.record_freeze(self.freeze))
        self.assertFalse(self.store.record_freeze(self.freeze))
        self.assertTrue(self.store.record_card(self.card))
        self.assertFalse(self.store.record_card(self.card))
        exported = self.store.export_candidate(self.candidate.candidate_id)
        self.assertEqual(self.candidate.candidate_id, exported["candidate"]["candidate_id"])
        self.assertEqual(1, len(exported["evaluations"]))
        self.assertEqual(1, len(exported["freeze_assessments"]))
        self.assertEqual(1, len(exported["policy_cards"]))
        counts = self.store.counts()
        self.assertEqual(1, counts["counterfactual_conclusions"])
        self.assertEqual(1, counts["v057_shadow_review_candidates"])
        self.assertEqual(0, counts["policy_candidate_route_interventions"])
        self.assertEqual(0, counts["policy_production_promotions"])

    def test_candidate_requires_persisted_predictor_evidence(self):
        with self.assertRaises(PolicyCandidateError):
            self.store.record_candidate(self.candidate)

    def test_audited_delete_cascades_payload_and_blocks_identity_reuse(self):
        self.persist_predictor_fixture()
        self.store.record_candidate(self.candidate)
        self.store.record_evaluation(self.evaluation)
        self.store.record_freeze(self.freeze)
        self.store.record_card(self.card)
        self.assertTrue(
            self.store.delete_candidate(
                self.candidate.candidate_id, reason_code="test_cleanup"
            )
        )
        self.assertIsNone(self.store.candidate(self.candidate.candidate_id))
        self.assertEqual(
            4,
            self.store.deletion(self.candidate.candidate_id)[
                "deleted_record_count"
            ],
        )
        self.assertFalse(
            self.store.delete_candidate(
                self.candidate.candidate_id, reason_code="test_cleanup"
            )
        )
        with self.assertRaises(PolicyCandidateError):
            self.store.record_candidate(self.candidate)

    def test_direct_mutation_and_unaudited_delete_are_rejected(self):
        self.persist_predictor_fixture()
        self.store.record_candidate(self.candidate)
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE policy_candidates SET routing_authority=1 "
                    "WHERE candidate_id=?",
                    (self.candidate.candidate_id,),
                )
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "DELETE FROM policy_candidates WHERE candidate_id=?",
                    (self.candidate.candidate_id,),
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
