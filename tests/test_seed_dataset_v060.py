"""Property tests for the deliberately synthetic v0.6.0 seed dataset."""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from costweave.benchmarks.contracts import (
    RoutingRecordV1,
    build_dataset_manifest,
    validate_routing_records,
)


SEED_PATH = Path(__file__).resolve().parents[1] / "costweave" / "benchmarks" / "v060_seed_routing_records.json"


class SeedDatasetV060Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        raw = json.loads(SEED_PATH.read_text(encoding="utf-8"))
        cls.records = validate_routing_records(tuple(RoutingRecordV1.from_dict(item) for item in raw))
        cls.by_case = {}
        for record in cls.records:
            cls.by_case.setdefault(record.case_id, []).append(record)

    def test_seed_has_expected_shape_and_partition_balance(self):
        self.assertEqual(36, len(self.records))
        self.assertEqual(12, len(self.by_case))
        self.assertEqual(
            {"seed-strong", "seed-balanced", "seed-cheap"},
            {record.model_id for record in self.records},
        )
        self.assertEqual(
            {"code", "research", "writing", "media"},
            {record.task_family for record in self.records},
        )
        self.assertEqual(
            {"train": 12, "calibration": 12, "test": 12},
            {partition: sum(record.partition == partition for record in self.records) for partition in ("train", "calibration", "test")},
        )

    def test_every_case_is_blind_safe_and_has_three_comparable_models(self):
        for case_id, records in self.by_case.items():
            self.assertEqual(3, len(records), case_id)
            self.assertEqual(1, len({record.prompt_hash for record in records}), case_id)
            self.assertEqual(1, len({record.partition for record in records}), case_id)
            self.assertEqual(1, len({record.source_dataset for record in records}), case_id)
            self.assertEqual(1, len({record.source_version for record in records}), case_id)
            self.assertEqual(1, len({record.price_snapshot_id for record in records}), case_id)
            self.assertTrue(all(record.latency_ms is not None for record in records), case_id)

    def test_seed_is_explicitly_e0_and_never_claim_ready(self):
        self.assertEqual({"E0-simulated"}, {record.evidence_level for record in self.records})
        manifest = build_dataset_manifest(
            self.records,
            source_uri="package://costweave/benchmarks/v060_seed_routing_records.json",
            license_id="MIT",
            created_at="2026-08-09T12:00:00Z",
        )
        self.assertEqual("E0-simulated", manifest.evidence_level)
        self.assertEqual(1.0, manifest.latency_coverage)
        self.assertEqual(36, manifest.record_count)
        self.assertEqual(12, manifest.case_count)

    def test_seed_contains_routing_signal_without_a_global_quality_oracle(self):
        best_models = set()
        non_best_models = set()
        accepted_counts = {model_id: 0 for model_id in {record.model_id for record in self.records}}
        strong_only_cases = []
        for records in self.by_case.values():
            best_quality = max(record.quality_score for record in records)
            best_models.update(record.model_id for record in records if record.quality_score == best_quality)
            non_best_models.update(record.model_id for record in records if record.quality_score < best_quality)
            for record in records:
                accepted_counts[record.model_id] += int(record.accepted)
            accepted_models = {record.model_id for record in records if record.accepted}
            if accepted_models == {"seed-strong"}:
                strong_only_cases.append(records[0].case_id)
        self.assertEqual({"seed-strong", "seed-balanced", "seed-cheap"}, best_models)
        self.assertEqual({"seed-strong", "seed-balanced", "seed-cheap"}, non_best_models)
        self.assertTrue(all(0 < count <= 12 for count in accepted_counts.values()))
        self.assertLess(accepted_counts["seed-balanced"], 12)
        self.assertLess(accepted_counts["seed-cheap"], 12)
        self.assertTrue(strong_only_cases)

    def test_seed_contains_only_hashes_and_seed_identifiers(self):
        for record in self.records:
            self.assertRegex(record.prompt_hash, r"^[0-9a-f]{64}$")
            self.assertRegex(record.response_hash, r"^[0-9a-f]{64}$")
            self.assertTrue(record.source_dataset.startswith("costweave-v060-"))
            self.assertTrue(record.model_id.startswith("seed-"))
            self.assertTrue(record.price_snapshot_id.startswith("seed-"))


if __name__ == "__main__":
    unittest.main()
