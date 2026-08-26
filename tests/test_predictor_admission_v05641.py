from __future__ import annotations

import tempfile
import unittest

from costweave.baseline_runner import build_seed_runner
from costweave.control_kernel import ControlKernel
from costweave.event_store import EventStore
from costweave.outcome_dataset import (
    GovernanceConfig,
    build_outcome_dataset,
)
from costweave.outcome_dataset_store import OutcomeDatasetStore
from costweave.predictor_admission import (
    DatasetEvidenceSummary,
    FrozenPredictionReceipt,
    PROFILE_DIRECTIONAL,
    PROFILE_FORMAL,
    PROFILE_INTERNAL,
    PredictorAdmissionError,
    PredictorAdmissionGate,
    PredictorFeatureKey,
    PredictorTrainingFrozenError,
    STATE_BLOCKED,
    STATE_CALIBRATION_CANDIDATE,
    STATE_DIRECTIONAL_ONLY,
    public_predictor_policy,
)
from costweave.predictor_admission_store import PredictorAdmissionStore


def summary(
    *,
    effective=200.0,
    evidence=None,
    families=None,
    source_training_allowed=True,
    partitions=None,
    **overrides,
):
    values = {
        "dataset_id": "dataset_" + "a" * 32,
        "dataset_content_hash": "b" * 64,
        "sample_count": int(effective),
        "effective_task_count": effective,
        "evidence_weights": evidence or {"E2": effective},
        "partition_counts": partitions
        or {"train": 100, "calibration": 50, "test": 50},
        "task_family_counts": families
        or {"code": 50.0, "research": 50.0, "data": 50.0, "ops": 50.0},
        "governance_passed": True,
        "source_training_allowed": source_training_allowed,
        "label_coverage": 1.0,
        "label_conflict_rate": 0.0,
        "project_leakage_count": 0,
        "cost_reconciliation_rate": 1.0,
        "candidate_route_capture_rate": 1.0,
        "selection_propensity_rate": 1.0,
        "version_traceability_rate": 1.0,
        "source_freeze_reasons": (),
    }
    values.update(overrides)
    return DatasetEvidenceSummary(**values)


def feature():
    return PredictorFeatureKey(
        task_type="code",
        risk_level="medium",
        difficulty_bucket="d3",
        deployment_id="deployment_local_qwen",
        model_id="qwen-test",
        provider="local",
        reasoning_effort="medium",
        prompt_revision="prompt-sha256-abc",
        tool_revision="tools-v1",
        catalog_revision="catalog-v9",
        outcome_schema="outcome-v1",
    )


class PredictorAdmissionGateTests(unittest.TestCase):
    def setUp(self):
        self.gate = PredictorAdmissionGate()

    def test_directional_profile_accepts_thirty_real_tasks_but_never_trains(self):
        result = self.gate.evaluate(
            summary(
                effective=30.0,
                evidence={"E2": 30.0},
                families={"code": 10.0, "data": 10.0, "research": 10.0},
                source_training_allowed=False,
                partitions={"train": 10, "calibration": 10, "test": 10},
            ),
            profile=PROFILE_DIRECTIONAL,
        )
        self.assertEqual(STATE_DIRECTIONAL_ONLY, result.state)
        self.assertFalse(result.training_allowed)
        self.assertFalse(result.routing_authority)
        self.assertIn("directional_profile_never_trains", result.freeze_reasons)

    def test_internal_profile_admits_only_governed_training_snapshot(self):
        result = self.gate.evaluate(summary(), profile=PROFILE_INTERNAL)
        self.assertEqual(STATE_CALIBRATION_CANDIDATE, result.state)
        self.assertTrue(result.training_allowed)
        self.assertFalse(result.policy_promotion_allowed)

    def test_formal_profile_requires_eight_large_families(self):
        result = self.gate.evaluate(
            summary(
                effective=1000.0,
                evidence={"E3": 1000.0},
                families={f"family-{index}": 125.0 for index in range(8)},
                partitions={"train": 600, "calibration": 200, "test": 200},
            ),
            profile=PROFILE_FORMAL,
        )
        self.assertEqual(STATE_CALIBRATION_CANDIDATE, result.state)

    def test_simulated_or_mixed_evidence_is_blocked(self):
        result = self.gate.evaluate(
            summary(evidence={"E0": 1.0, "E2": 199.0}),
            profile=PROFILE_INTERNAL,
        )
        self.assertEqual(STATE_BLOCKED, result.state)
        self.assertIn("real_evidence_only", result.freeze_reasons)

    def test_source_dataset_freeze_is_not_bypassed(self):
        result = self.gate.evaluate(
            summary(
                source_training_allowed=False,
                source_freeze_reasons=("source_evaluation_failed",),
            ),
            profile=PROFILE_INTERNAL,
        )
        self.assertEqual(STATE_BLOCKED, result.state)
        self.assertIn("source_evaluation_failed", result.freeze_reasons)
        with self.assertRaises(PredictorTrainingFrozenError):
            self.gate.assert_training_allowed(result)

    def test_missing_partition_and_incomplete_cost_are_explainable(self):
        result = self.gate.evaluate(
            summary(
                partitions={"train": 150, "calibration": 50, "test": 0},
                cost_reconciliation_rate=0.99,
            )
        )
        self.assertEqual(STATE_BLOCKED, result.state)
        self.assertIn("all_partitions_present", result.freeze_reasons)
        self.assertIn("cost_reconciled", result.freeze_reasons)

    def test_report_id_is_replay_deterministic(self):
        first = self.gate.evaluate(summary())
        second = self.gate.evaluate(summary())
        self.assertEqual(first.report_id, second.report_id)
        self.assertEqual(first.to_dict(), second.to_dict())


class PredictorReplayContractTests(unittest.TestCase):
    def test_feature_fingerprint_is_stable_and_prompt_body_is_absent(self):
        first = feature()
        self.assertEqual(first.fingerprint, feature().fingerprint)
        self.assertNotIn("prompt", first.to_dict())
        self.assertIn("prompt_revision", first.to_dict())

    def test_feature_rejects_obvious_secret_material(self):
        values = feature().to_dict()
        values["tool_revision"] = "api_key=unsafe-value"
        with self.assertRaises(PredictorAdmissionError):
            PredictorFeatureKey(**values)

    def test_frozen_receipt_invents_no_probability_and_has_no_authority(self):
        admission = PredictorAdmissionGate().evaluate(
            summary(
                effective=30.0,
                evidence={"E2": 30.0},
                families={"a": 10.0, "b": 10.0, "c": 10.0},
                source_training_allowed=False,
                partitions={"train": 10, "calibration": 10, "test": 10},
            ),
            profile=PROFILE_DIRECTIONAL,
        )
        receipt = FrozenPredictionReceipt.create(feature(), admission=admission)
        self.assertIsNone(receipt.outcome_probabilities)
        self.assertEqual((0.0, 1.0), receipt.uncertainty_interval)
        self.assertFalse(receipt.learned)
        self.assertFalse(receipt.routing_authority)
        self.assertEqual(
            receipt.receipt_id,
            FrozenPredictionReceipt.create(feature(), admission=admission).receipt_id,
        )

    def test_public_policy_is_default_frozen_and_zero_call(self):
        policy = public_predictor_policy()
        self.assertEqual("frozen", policy["current_state"])
        self.assertFalse(policy["training_enabled_by_default"])
        self.assertFalse(policy["policy_promotion_enabled"])
        self.assertFalse(policy["external_calls_required"])
        self.assertFalse(policy["invented_probabilities_allowed"])


class PredictorAdmissionPersistenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = build_seed_runner().run().to_dict()
        cls.dataset = build_outcome_dataset(
            source,
            GovernanceConfig(
                privacy_salt="predictor-admission-test-salt",
                created_at="2026-08-02T00:00:00Z",
            ),
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"
        EventStore(self.path)
        OutcomeDatasetStore(self.path).record(self.dataset)
        self.store = PredictorAdmissionStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_simulated_dataset_is_blocked_and_persisted_idempotently(self):
        report = PredictorAdmissionGate().evaluate_dataset(self.dataset)
        self.assertEqual(STATE_BLOCKED, report.state)
        self.assertTrue(self.store.record_report(report))
        self.assertFalse(self.store.record_report(report))
        self.assertEqual(report.to_dict(), self.store.report(report.report_id))
        self.assertEqual(1, self.store.counts()["predictor_admission_reports"])
        self.assertEqual(0, self.store.counts()["calibration_candidates"])

    def test_frozen_receipt_is_persisted_and_replayable(self):
        report = PredictorAdmissionGate().evaluate_dataset(self.dataset)
        self.store.record_report(report)
        receipt = FrozenPredictionReceipt.create(feature(), admission=report)
        self.assertTrue(self.store.record_receipt(receipt))
        self.assertFalse(self.store.record_receipt(receipt))
        self.assertEqual(receipt.to_dict(), self.store.receipt(receipt.receipt_id))
        counts = self.store.counts()
        self.assertEqual(1, counts["frozen_prediction_receipts"])
        self.assertEqual(0, counts["authoritative_learned_predictions"])

    def test_control_kernel_automatically_records_all_admission_profiles(self):
        other_path = f"{self.temp.name}/kernel.sqlite3"
        kernel = ControlKernel(other_path)
        self.assertTrue(kernel.record_outcome_dataset(self.dataset))
        counts = kernel.predictor_admission_counts()
        self.assertEqual(3, counts["predictor_admission_reports"])
        self.assertEqual(0, counts["calibration_candidates"])


if __name__ == "__main__":
    unittest.main()
