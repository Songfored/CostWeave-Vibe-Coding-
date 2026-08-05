from __future__ import annotations

import tempfile
import unittest
import sqlite3
from dataclasses import replace

from costweave.baseline_runner import build_seed_runner
from costweave.event_store import EventStore
from costweave.outcome_dataset import GovernanceConfig, build_outcome_dataset
from costweave.outcome_dataset_store import OutcomeDatasetStore
from costweave.predictor_admission import (
    DatasetEvidenceSummary,
    PredictorAdmissionError,
    PredictorAdmissionGate,
)
from costweave.predictor_admission_store import PredictorAdmissionStore
from costweave.predictor_evaluation import (
    OUTCOME_CATEGORIES,
    OUTCOME_FAILED,
    OUTCOME_FIRST_PASS,
    PredictionEvaluationObservation,
    PredictorCandidateManifest,
    PredictorEvaluationError,
    PredictorEvaluationGate,
    compare_challenger,
    compute_metrics,
    public_evaluation_policy,
)
from costweave.predictor_evaluation_store import PredictorEvaluationStore


DATASET_HASH = "d" * 64
COHORT_HASH = "c" * 64


def admitted_report():
    summary = DatasetEvidenceSummary(
        dataset_id="dataset_" + "a" * 32,
        dataset_content_hash=DATASET_HASH,
        sample_count=200,
        effective_task_count=200.0,
        evidence_weights={"E3": 200.0},
        partition_counts={"train": 120, "calibration": 40, "test": 40},
        task_family_counts={
            "code": 50.0,
            "data": 50.0,
            "research": 50.0,
            "ops": 50.0,
        },
        governance_passed=True,
        source_training_allowed=True,
        label_coverage=1.0,
        label_conflict_rate=0.0,
        project_leakage_count=0,
        cost_reconciliation_rate=1.0,
        candidate_route_capture_rate=1.0,
        selection_propensity_rate=1.0,
        version_traceability_rate=1.0,
    )
    return PredictorAdmissionGate().evaluate(summary)


def blocked_report(*, dataset_id=None, content_hash=None):
    summary = DatasetEvidenceSummary(
        dataset_id=dataset_id or "dataset_" + "b" * 32,
        dataset_content_hash=content_hash or "e" * 64,
        sample_count=35,
        effective_task_count=35.0,
        evidence_weights={"E0": 35.0},
        partition_counts={"train": 15, "calibration": 10, "test": 10},
        task_family_counts={"code": 15.0, "data": 10.0, "research": 10.0},
        governance_passed=True,
        source_training_allowed=False,
        label_coverage=1.0,
        label_conflict_rate=0.0,
        project_leakage_count=0,
        cost_reconciliation_rate=1.0,
        candidate_route_capture_rate=1.0,
        selection_propensity_rate=1.0,
        version_traceability_rate=1.0,
        source_freeze_reasons=("simulated_evidence_not_trainable",),
    )
    return PredictorAdmissionGate().evaluate(summary)


def artifact(*, admission=None, payload="1" * 64):
    return PredictorCandidateManifest.create(
        admission or admitted_report(),
        method="hierarchical_beta",
        feature_schema_version="outcome-features-v1",
        target_schema_version="five-outcomes-v1",
        code_revision="predictor-fixture-v1",
        training_config_hash="2" * 64,
        model_payload_hash=payload,
        created_at="2026-08-02T00:00:00Z",
    )


def observations(
    *,
    evidence="E3-independent-real",
    count=200,
    cost=1.0,
    latency=100.0,
):
    result = []
    for index in range(count):
        failed = index < max(1, count // 5)
        predicted = (
            {
                OUTCOME_CATEGORIES[0]: 0.02,
                OUTCOME_CATEGORIES[1]: 0.01,
                OUTCOME_CATEGORIES[2]: 0.01,
                OUTCOME_CATEGORIES[3]: 0.01,
                OUTCOME_FAILED: 0.95,
            }
            if failed
            else {
                OUTCOME_CATEGORIES[0]: 0.80,
                OUTCOME_CATEGORIES[1]: 0.10,
                OUTCOME_CATEGORIES[2]: 0.03,
                OUTCOME_CATEGORIES[3]: 0.02,
                OUTCOME_FAILED: 0.05,
            }
        )
        covered = index >= max(1, count // 20)
        result.append(
            PredictionEvaluationObservation(
                observation_id=f"predobs_{index:032x}",
                anonymous_sample_id=f"sample_{index:032x}",
                anonymous_project_id=f"project_{index // 5:032x}",
                partition="test",
                evidence_level=evidence,
                risk_level="high" if failed else "medium",
                feature_fingerprint=f"feature_{index:032x}",
                dataset_content_hash=DATASET_HASH,
                predicted_outcomes=predicted,
                actual_outcome=OUTCOME_FAILED if failed else OUTCOME_FIRST_PASS,
                difficulty_score=0.90 if failed else 0.10,
                predicted_total_cost_p50=cost * (1.0 if covered else 0.70),
                predicted_total_cost_p95=cost * (1.20 if covered else 0.80),
                actual_total_cost=cost,
                predicted_latency_p50_ms=latency * (1.0 if covered else 0.70),
                predicted_latency_p95_ms=(
                    latency * (1.20 if covered else 0.80)
                ),
                actual_latency_ms=latency,
            )
        )
    return result


class PredictorEvaluationContractTests(unittest.TestCase):
    def test_frozen_admission_cannot_create_artifact(self):
        with self.assertRaises(PredictorEvaluationError):
            artifact(admission=blocked_report())

    def test_artifact_is_deterministic_and_never_authoritative(self):
        first = artifact()
        self.assertEqual(first.artifact_id, artifact().artifact_id)
        self.assertEqual("frozen_candidate", first.status)
        self.assertFalse(first.routing_authority)
        self.assertFalse(first.policy_promotion_allowed)

    def test_prediction_requires_exact_five_way_distribution(self):
        item = observations(count=20)[0]
        invalid = dict(item.predicted_outcomes)
        invalid.pop(OUTCOME_FAILED)
        with self.assertRaises(PredictorEvaluationError):
            replace(item, predicted_outcomes=invalid)

    def test_observation_rejects_unhashed_project_identifier(self):
        item = observations(count=20)[0]
        with self.assertRaises(PredictorEvaluationError):
            replace(item, anonymous_project_id="project_customer-name")

    def test_metrics_cover_calibration_risk_cost_and_latency(self):
        metrics = compute_metrics(observations())
        self.assertLessEqual(metrics["brier"], 0.18)
        self.assertLessEqual(metrics["ece"], 0.08)
        self.assertEqual(0.95, metrics["cost_p95_coverage"])
        self.assertEqual(0.95, metrics["latency_p95_coverage"])
        self.assertGreaterEqual(metrics["difficulty_failure_spearman"], 0.70)
        self.assertIsNotNone(metrics["cost_per_accepted_task"])

    def test_e0_fixture_is_blocked_even_when_metrics_are_good(self):
        report = PredictorEvaluationGate().evaluate(
            artifact(),
            admitted_report(),
            observations(evidence="E0-fixture"),
            cohort_hash=COHORT_HASH,
        )
        self.assertEqual("blocked", report.state)
        self.assertFalse(report.offline_evaluation_passed)
        self.assertFalse(report.eligible_for_shadow)
        self.assertIn("real_evidence_only", report.freeze_reasons)

    def test_e2_can_pass_offline_but_cannot_enter_shadow(self):
        report = PredictorEvaluationGate().evaluate(
            artifact(),
            admitted_report(),
            observations(evidence="E2-real-validated"),
            cohort_hash=COHORT_HASH,
        )
        self.assertEqual("offline_pass", report.state)
        self.assertTrue(report.offline_evaluation_passed)
        self.assertFalse(report.eligible_for_shadow)
        self.assertIn("shadow_requires_e3_or_higher", report.freeze_reasons)

    def test_e3_passing_report_is_only_a_shadow_candidate(self):
        report = PredictorEvaluationGate().evaluate(
            artifact(), admitted_report(), observations(), cohort_hash=COHORT_HASH
        )
        self.assertEqual("shadow_candidate", report.state)
        self.assertTrue(report.eligible_for_shadow)
        self.assertFalse(report.routing_authority)
        self.assertFalse(report.policy_promotion_allowed)

    def test_small_or_duplicate_cohort_is_blocked(self):
        small = observations(count=50)
        small[1] = replace(
            small[1], anonymous_sample_id=small[0].anonymous_sample_id
        )
        report = PredictorEvaluationGate().evaluate(
            artifact(), admitted_report(), small, cohort_hash=COHORT_HASH
        )
        self.assertEqual("blocked", report.state)
        self.assertIn("minimum_effective_tasks", report.freeze_reasons)
        self.assertIn("unique_samples", report.freeze_reasons)

    def test_overwide_p95_is_rejected_as_miscalibrated(self):
        items = [
            replace(
                item,
                predicted_total_cost_p95=item.actual_total_cost * 2,
                predicted_latency_p95_ms=item.actual_latency_ms * 2,
            )
            for item in observations()
        ]
        report = PredictorEvaluationGate().evaluate(
            artifact(), admitted_report(), items, cohort_hash=COHORT_HASH
        )
        self.assertEqual("offline_fail", report.state)
        self.assertIn("cost_p95_coverage", report.freeze_reasons)
        self.assertIn("latency_p95_coverage", report.freeze_reasons)

    def test_report_replays_deterministically(self):
        gate = PredictorEvaluationGate()
        first = gate.evaluate(
            artifact(), admitted_report(), observations(), cohort_hash=COHORT_HASH
        )
        second = gate.evaluate(
            artifact(), admitted_report(), observations(), cohort_hash=COHORT_HASH
        )
        self.assertEqual(first.report_id, second.report_id)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_challenger_requires_noninferiority_and_economic_value(self):
        gate = PredictorEvaluationGate()
        champion = gate.evaluate(
            artifact(payload="3" * 64),
            admitted_report(),
            observations(cost=1.0),
            cohort_hash=COHORT_HASH,
        )
        challenger = gate.evaluate(
            artifact(payload="4" * 64),
            admitted_report(),
            observations(cost=0.90),
            cohort_hash=COHORT_HASH,
        )
        comparison = compare_challenger(champion, challenger)
        self.assertTrue(comparison.eligible_for_shadow_comparison)
        self.assertFalse(comparison.routing_authority)
        self.assertFalse(comparison.policy_promotion_allowed)
        self.assertGreaterEqual(
            comparison.deltas["cost_improvement_ratio"], 0.03
        )

    def test_challenger_comparison_rejects_different_cohort(self):
        gate = PredictorEvaluationGate()
        champion = gate.evaluate(
            artifact(payload="5" * 64),
            admitted_report(),
            observations(),
            cohort_hash=COHORT_HASH,
        )
        challenger = gate.evaluate(
            artifact(payload="6" * 64),
            admitted_report(),
            observations(cost=0.8),
            cohort_hash="f" * 64,
        )
        comparison = compare_challenger(champion, challenger)
        self.assertFalse(comparison.eligible_for_shadow_comparison)
        self.assertFalse(comparison.checks["same_cohort"])

    def test_public_policy_declares_fixture_and_promotion_boundaries(self):
        policy = public_evaluation_policy()
        self.assertEqual("v0.5.6.4.2", policy["schema_version"])
        self.assertFalse(policy["offline_fixture_metrics_are_claimable"])
        self.assertFalse(policy["policy_promotion_enabled"])
        self.assertFalse(policy["external_calls_required"])


class PredictorEvaluationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        self.health = EventStore(self.path).health()
        self.store = PredictorEvaluationStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_creates_empty_frozen_registry(self):
        self.assertEqual("v5.8.2", self.health["runtime_schema"])
        self.assertEqual(27, self.health["migrations"])
        counts = self.store.counts()
        self.assertEqual(0, counts["predictor_candidate_artifacts"])
        self.assertEqual(0, counts["predictor_shadow_candidates"])
        self.assertEqual(0, counts["predictor_production_promotions"])

    def test_artifact_cannot_be_persisted_without_admitted_dataset(self):
        with self.assertRaises(PredictorEvaluationError):
            self.store.record_artifact(artifact())

    def test_admission_store_cannot_bypass_source_dataset_freeze(self):
        dataset = build_outcome_dataset(
            build_seed_runner().run().to_dict(),
            GovernanceConfig(
                privacy_salt="v05642-admission-bypass-test",
                created_at="2026-08-02T00:00:00Z",
            ),
        )
        OutcomeDatasetStore(self.path).record(dataset)
        forged = DatasetEvidenceSummary(
            dataset_id=dataset.dataset_id,
            dataset_content_hash=dataset.content_hash,
            sample_count=200,
            effective_task_count=200.0,
            evidence_weights={"E3": 200.0},
            partition_counts={"train": 120, "calibration": 40, "test": 40},
            task_family_counts={
                "code": 50.0,
                "data": 50.0,
                "research": 50.0,
                "ops": 50.0,
            },
            governance_passed=True,
            source_training_allowed=True,
            label_coverage=1.0,
            label_conflict_rate=0.0,
            project_leakage_count=0,
            cost_reconciliation_rate=1.0,
            candidate_route_capture_rate=1.0,
            selection_propensity_rate=1.0,
            version_traceability_rate=1.0,
        )
        report = PredictorAdmissionGate().evaluate(forged)
        with self.assertRaises(PredictorAdmissionError):
            PredictorAdmissionStore(self.path).record_report(report)

        blocked = PredictorAdmissionGate().evaluate_dataset(dataset)
        PredictorAdmissionStore(self.path).record_report(blocked)
        connection = sqlite3.connect(self.path)
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE predictor_admission_reports "
                    "SET policy_promotion_allowed=1 WHERE report_id=?",
                    (blocked.report_id,),
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
