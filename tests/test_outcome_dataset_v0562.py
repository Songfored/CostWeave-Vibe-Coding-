from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from costweave.baseline_runner import build_seed_runner
from costweave.event_store import EventStore
from costweave.outcome_dataset import (
    GovernanceConfig,
    LABEL_ACCEPTED,
    LABEL_REJECTED,
    LABEL_SOURCE_HUMAN_BLIND,
    LABEL_SOURCE_USER,
    LabelEvidence,
    OUTCOME_DATASET_SCHEMA_VERSION,
    OutcomeGovernanceError,
    TrainingFrozenError,
    assert_training_ready,
    build_outcome_dataset,
    export_outcome_dataset,
    resolve_labels,
)
from costweave.outcome_dataset_store import OutcomeDatasetStore


CREATED_AT = "2026-07-30T00:00:00Z"
SALT = "costweave-v0562-test-salt"


class OutcomeDatasetBuilderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = build_seed_runner().run().to_dict()
        cls.config = GovernanceConfig(
            privacy_salt=SALT,
            created_at=CREATED_AT,
        )
        cls.dataset = build_outcome_dataset(cls.source, cls.config)

    def test_builds_complete_observation_matrix(self):
        manifest = self.dataset.manifest()
        self.assertEqual(175, manifest["sample_count"])
        self.assertEqual(35.0, manifest["effective_task_count"])
        self.assertEqual(
            {"train": 75, "calibration": 50, "test": 50},
            manifest["partition_counts"],
        )

    def test_build_is_deterministic(self):
        replay = build_outcome_dataset(self.source, self.config)
        self.assertEqual(self.dataset.dataset_id, replay.dataset_id)
        self.assertEqual(self.dataset.content_hash, replay.content_hash)

    def test_different_privacy_salt_changes_anonymous_ids(self):
        changed = build_outcome_dataset(
            self.source,
            GovernanceConfig(
                privacy_salt="another-v0562-privacy-salt",
                created_at=CREATED_AT,
            ),
        )
        self.assertNotEqual(
            self.dataset.samples[0].anonymous_project_id,
            changed.samples[0].anonymous_project_id,
        )

    def test_learning_copy_excludes_raw_text_and_identifiers(self):
        sample_raw = json.dumps(
            [item.to_dict() for item in self.dataset.samples],
            ensure_ascii=False,
        )
        for task in self.source["tasks"]:
            self.assertNotIn(task["user_goal"], sample_raw)
            self.assertNotIn(task["project_id"], sample_raw)
            self.assertNotIn(task["benchmark_id"], sample_raw)
        self.assertNotIn("reference_answer", sample_raw)

    def test_route_receipt_is_sanitized_but_attributed(self):
        route = next(
            item.selected_route
            for item in self.dataset.samples
            if item.selected_route
        )
        self.assertIn("model_id", route)
        self.assertIn("provider", route)
        self.assertIn("route_receipt_hash", route)
        self.assertNotIn("task_id", route)
        self.assertNotIn("receipt_id", route)
        self.assertNotIn("rationale", route)

    def test_simulated_seed_is_governed_but_frozen(self):
        quality = self.dataset.quality_report
        self.assertTrue(quality["governance_passed"])
        self.assertFalse(quality["training_allowed"])
        self.assertIn(
            "simulated_evidence_not_trainable",
            quality["freeze_reasons"],
        )
        self.assertIn(
            "insufficient_effective_sample_size",
            quality["freeze_reasons"],
        )
        self.assertIn(
            "candidate_route_capture_incomplete",
            quality["freeze_reasons"],
        )

    def test_training_entry_hard_fails_when_frozen(self):
        with self.assertRaises(TrainingFrozenError):
            assert_training_ready(self.dataset)

    def test_quality_report_reconciles_every_cost(self):
        quality = self.dataset.quality_report
        self.assertEqual(175, quality["cost_reconciled_count"])
        self.assertEqual(1.0, quality["cost_reconciliation_rate"])

    def test_route_variants_do_not_amplify_effective_sample_size(self):
        weights: dict[str, float] = {}
        for sample in self.dataset.samples:
            weights[sample.dedupe_group] = (
                weights.get(sample.dedupe_group, 0.0)
                + sample.sample_weight
            )
        self.assertEqual(35, len(weights))
        self.assertTrue(
            all(abs(value - 1.0) < 1e-8 for value in weights.values())
        )
        self.assertEqual(
            140, self.dataset.quality_report["route_variant_count"]
        )

    def test_project_partition_leakage_is_detected_and_frozen(self):
        report = copy.deepcopy(self.source)
        train = next(
            task for task in report["tasks"]
            if task["partition"] == "train"
        )
        test = next(
            task for task in report["tasks"]
            if task["partition"] == "test"
        )
        old_project = test["project_id"]
        test["project_id"] = train["project_id"]
        for observation in report["observations"]:
            if observation["project_id"] == old_project:
                observation["project_id"] = train["project_id"]
        dataset = build_outcome_dataset(report, self.config)
        self.assertFalse(dataset.quality_report["governance_passed"])
        self.assertEqual(
            1, dataset.quality_report["project_leakage_count"]
        )
        self.assertIn(
            "project_partition_leakage",
            dataset.quality_report["freeze_reasons"],
        )

    def test_duplicate_observation_ids_are_rejected(self):
        report = copy.deepcopy(self.source)
        report["observations"].append(
            copy.deepcopy(report["observations"][0])
        )
        with self.assertRaises(OutcomeGovernanceError):
            build_outcome_dataset(report, self.config)

    def test_unknown_task_reference_is_rejected(self):
        report = copy.deepcopy(self.source)
        report["observations"][0]["task_id"] = "benchmark_missing"
        with self.assertRaises(OutcomeGovernanceError):
            build_outcome_dataset(report, self.config)

    def test_invalid_or_short_privacy_salt_is_rejected(self):
        with self.assertRaises(OutcomeGovernanceError):
            GovernanceConfig(privacy_salt="short")

    def test_source_report_is_not_mutated(self):
        raw = json.dumps(self.source, sort_keys=True)
        build_outcome_dataset(self.source, self.config)
        self.assertEqual(raw, json.dumps(self.source, sort_keys=True))

    def test_export_is_immutable_and_contains_data_card(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = export_outcome_dataset(self.dataset, directory)
            self.assertEqual(
                self.dataset.dataset_id,
                json.loads(paths["manifest"].read_text())["dataset_id"],
            )
            self.assertTrue(paths["data_card"].exists())
            export_outcome_dataset(self.dataset, directory)
            paths["manifest"].write_text("different", encoding="utf-8")
            with self.assertRaises(OutcomeGovernanceError):
                export_outcome_dataset(self.dataset, directory)

    def test_schema_and_sample_tier_are_explicit(self):
        self.assertEqual(
            OUTCOME_DATASET_SCHEMA_VERSION,
            self.dataset.schema_version,
        )
        self.assertEqual(
            "seed_validation",
            self.dataset.quality_report["sample_tier"],
        )
        self.assertFalse(
            self.dataset.quality_report["commercial_claims_allowed"]
        )


class LabelGovernanceTests(unittest.TestCase):
    def event(
        self,
        *,
        source=LABEL_SOURCE_USER,
        verdict=LABEL_ACCEPTED,
        independent=False,
        recorded_at="2026-07-30T01:00:00Z",
    ):
        return LabelEvidence.create(
            sample_id="sample_" + "a" * 32,
            source=source,
            verdict=verdict,
            independent=independent,
            blinded=source == LABEL_SOURCE_HUMAN_BLIND,
            delayed=True,
            evidence_level="E1-user",
            evaluator_revision="reviewer-v1",
            evidence_refs=("evidence_123",),
            recorded_at=recorded_at,
        )

    def test_unverified_positive_is_not_a_success_label(self):
        resolved = resolve_labels((self.event(),))
        self.assertEqual("unlabelled", resolved["state"])
        self.assertIsNone(resolved["success"])

    def test_independent_positive_can_label_success(self):
        resolved = resolve_labels((self.event(independent=True),))
        self.assertEqual("success", resolved["state"])
        self.assertTrue(resolved["success"])

    def test_negative_terminal_feedback_labels_failure(self):
        resolved = resolve_labels(
            (self.event(verdict=LABEL_REJECTED),)
        )
        self.assertEqual("failure", resolved["state"])
        self.assertFalse(resolved["success"])

    def test_conflicting_labels_are_preserved(self):
        positive = self.event(independent=True)
        negative = self.event(
            verdict=LABEL_REJECTED,
            recorded_at="2026-07-30T02:00:00Z",
        )
        resolved = resolve_labels((positive, negative))
        self.assertEqual("conflict", resolved["state"])
        self.assertTrue(resolved["conflict"])
        self.assertIsNone(resolved["success"])

    def test_label_event_identity_is_deterministic(self):
        first = self.event(independent=True)
        second = self.event(independent=True)
        self.assertEqual(first.label_event_id, second.label_event_id)


class OutcomeDatasetStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = build_seed_runner().run().to_dict()
        cls.dataset = build_outcome_dataset(
            cls.source,
            GovernanceConfig(
                privacy_salt=SALT,
                created_at=CREATED_AT,
                ttl_days=1,
            ),
        )

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "runtime.sqlite3"
        self.health = EventStore(self.path).health()
        self.store = OutcomeDatasetStore(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_migration_15_and_runtime_schema(self):
        self.assertEqual("v5.6.4.4", self.health["runtime_schema"])
        self.assertEqual(20, self.health["migrations"])
        with closing(sqlite3.connect(self.path)) as connection:
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("outcome_datasets", tables)
        self.assertIn("outcome_learning_samples", tables)
        self.assertIn("outcome_label_events", tables)
        self.assertIn("outcome_deletion_audits", tables)

    def test_dataset_persistence_is_idempotent(self):
        self.assertTrue(self.store.record(self.dataset))
        self.assertFalse(self.store.record(self.dataset))
        self.assertEqual(
            {
                "outcome_datasets": 1,
                "outcome_learning_samples": 175,
                "outcome_label_events": 175,
                "outcome_deletion_audits": 0,
            },
            self.store.counts(),
        )

    def test_delayed_feedback_is_append_only_and_idempotent(self):
        self.store.record(self.dataset)
        target = self.dataset.samples[0]
        event = LabelEvidence.create(
            sample_id=target.sample_id,
            source=LABEL_SOURCE_USER,
            verdict=LABEL_REJECTED,
            independent=False,
            blinded=False,
            delayed=True,
            evidence_level="E1-user",
            evaluator_revision="feedback-v1",
            evidence_refs=("feedback_123",),
            recorded_at="2026-07-30T12:00:00Z",
        )
        self.assertTrue(self.store.append_label(event))
        self.assertFalse(self.store.append_label(event))
        materialized = self.store.sample(target.sample_id)
        self.assertIsNotNone(materialized)
        self.assertEqual(2, len(materialized.label_events))
        self.assertEqual("conflict", materialized.resolved_label["state"])

    def test_deletion_removes_learning_copy_but_keeps_audit(self):
        self.store.record(self.dataset)
        target = self.dataset.samples[0]
        self.assertTrue(
            self.store.delete_learning_copy(
                target.sample_id,
                reason_code="subject_request",
                requested_at="2026-07-30T12:00:00Z",
            )
        )
        self.assertIsNone(self.store.sample(target.sample_id))
        audits = self.store.deletion_audits(self.dataset.dataset_id)
        self.assertEqual(1, len(audits))
        self.assertEqual(target.sample_id, audits[0]["sample_id"])
        self.assertNotIn("sample_json", audits[0])
        self.assertFalse(
            self.store.delete_learning_copy(
                target.sample_id,
                reason_code="subject_request",
                requested_at="2026-07-30T12:00:00Z",
            )
        )

    def test_ttl_purge_is_audited(self):
        self.store.record(self.dataset)
        deleted = self.store.purge_expired(
            now="2026-07-31T00:00:00Z"
        )
        self.assertEqual(175, deleted)
        self.assertEqual(0, self.store.counts()["outcome_learning_samples"])
        self.assertEqual(
            175, self.store.counts()["outcome_deletion_audits"]
        )

    def test_store_training_gate_hard_fails(self):
        self.store.record(self.dataset)
        with self.assertRaises(TrainingFrozenError):
            self.store.training_snapshot(self.dataset.dataset_id)


if __name__ == "__main__":
    unittest.main()
