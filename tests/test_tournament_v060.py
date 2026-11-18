"""Offline tournament runner tests for CostWeave v0.6.0."""

from __future__ import annotations

import json
import unittest
from dataclasses import replace
from pathlib import Path

from costweave.benchmarks.contracts import RoutingRecordV1, build_dataset_manifest
from costweave.benchmarks.policies import DeterministicRandomPolicy, FixedModelPolicy, RecordedDecisionPolicy
from costweave.benchmarks.tournament import TournamentError, TournamentRunner, TournamentSpecV1


SEED_PATH = Path(__file__).resolve().parents[1] / "costweave" / "benchmarks" / "v060_seed_routing_records.json"


class TournamentV060Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.records = tuple(RoutingRecordV1.from_dict(item) for item in json.loads(SEED_PATH.read_text(encoding="utf-8")))
        cls.manifest = build_dataset_manifest(
            cls.records,
            source_uri="package://costweave/benchmarks/v060_seed_routing_records.json",
            license_id="MIT",
            created_at="2026-08-09T00:00:00Z",
        )

    def _spec(self, **overrides) -> TournamentSpecV1:
        values = {
            "project_id": "local-default",
            "dataset_id": self.manifest.dataset_id,
            "partition": "test",
            "strong_model_id": "seed-strong",
            "cheap_model_id": "seed-cheap",
            "baseline_policy_id": "strong-only",
            "acceptance_threshold": 0.8,
            "quality_weight": 1.0,
            "cost_weight": 0.1,
            "latency_weight": 0.01,
            "bootstrap_samples": 100,
            "random_seed": 600,
            "minimum_claim_cases": 30,
            "registered_at": "2026-08-09T00:00:00Z",
        }
        values.update(overrides)
        return TournamentSpecV1(**values)

    @staticmethod
    def _policies():
        return (
            FixedModelPolicy("seed-strong", policy_id="strong-only"),
            FixedModelPolicy("seed-cheap", policy_id="cheap-only"),
            FixedModelPolicy("seed-balanced", policy_id="balanced-only"),
            DeterministicRandomPolicy(600, policy_id="random-seed-600"),
        )

    def test_spec_round_trip_and_timestamp_independent_id(self):
        spec = self._spec()
        self.assertEqual(spec, TournamentSpecV1.from_dict(spec.to_dict()))
        self.assertEqual(spec.tournament_id, replace(spec, registered_at="2026-08-10T00:00:00Z").tournament_id)
        with self.assertRaises(ValueError):
            self._spec(strong_model_id="seed-cheap")
        with self.assertRaises(ValueError):
            self._spec(bootstrap_samples=99)
        with self.assertRaises(ValueError):
            self._spec(minimum_claim_cases=29)

    def test_old_spec_keeps_its_identity_but_new_runs_use_the_p95_revision(self):
        current = self._spec()
        historical = replace(current, policy_revision="decision-intelligence-tournament-v1")
        self.assertEqual(historical, TournamentSpecV1.from_dict(historical.to_dict()))
        self.assertNotEqual(historical.tournament_id, current.tournament_id)
        with self.assertRaisesRegex(TournamentError, "policy revision"):
            TournamentRunner().run(
                spec=historical, manifest=self.manifest, records=self.records, policies=self._policies()
            )

    def test_runner_is_offline_deterministic_and_report_hash_excludes_generated_at(self):
        runner = TournamentRunner()
        first = runner.run(spec=self._spec(), manifest=self.manifest, records=self.records, policies=self._policies(), generated_at="2026-08-09T01:00:00Z")
        second = runner.run(spec=self._spec(), manifest=self.manifest, records=self.records, policies=self._policies(), generated_at="2026-08-09T02:00:00Z")
        self.assertEqual(first.report_hash or first.compute_report_hash(), second.report_hash or second.compute_report_hash())
        self.assertEqual(0, first.network_calls)
        self.assertEqual(0, first.provider_calls)
        self.assertEqual({"strong-only", "cheap-only", "balanced-only", "random-seed-600"}, set(first.policy_metrics))
        self.assertEqual(3, len(first.claim_gates))
        self.assertEqual("insufficient_sample", first.claim_gates["cheap-only"]["status"])
        self.assertFalse(first.claim_gates["cheap-only"]["passed"])
        rendered = json.dumps(first.to_dict(), ensure_ascii=False)
        self.assertNotIn("prompt-", rendered)
        self.assertNotIn("answer-", rendered)

    def test_report_round_trip_validates_hash(self):
        report = TournamentRunner().run(spec=self._spec(), manifest=self.manifest, records=self.records, policies=self._policies())
        from costweave.benchmarks.tournament import TournamentReportV1

        restored = TournamentReportV1.from_dict(report.to_dict())
        self.assertEqual(report.to_dict(), restored.to_dict())
        with self.assertRaises(ValueError):
            TournamentReportV1.from_dict({**report.to_dict(), "report_hash": "a" * 64})

    def _run_latency_comparison(self, baseline_ms, candidate_ms, **spec_overrides):
        records = []
        for index, (baseline, candidate) in enumerate(zip(baseline_ms, candidate_ms, strict=True)):
            case_id = f"latency-{index:03d}"
            for model, latency_ms, cost in (("seed-strong", baseline, 0.02), ("seed-cheap", candidate, 0.01)):
                records.append(replace(
                    self.records[0],
                    source_record_id=f"{case_id}-{model}",
                    case_id=case_id,
                    partition="test",
                    prompt_hash=f"{index:064x}",
                    model_id=model,
                    quality_score=1.0,
                    accepted=True,
                    cost_usd=cost,
                    latency_ms=latency_ms,
                    evidence_level="E1-public-replay",
                ))
        manifest = build_dataset_manifest(
            records,
            source_uri="fixture://p95-latency",
            license_id="MIT",
            created_at=self.manifest.created_at,
        )
        return TournamentRunner().run(
            spec=self._spec(dataset_id=manifest.dataset_id, **spec_overrides),
            manifest=manifest,
            records=records,
            policies=(FixedModelPolicy("seed-strong", "strong-only"), FixedModelPolicy("seed-cheap", "cheap-only")),
        )

    def test_faster_average_does_not_hide_a_doubled_p95(self):
        report = self._run_latency_comparison([100] * 100, [10] * 94 + [200] * 6)
        self.assertEqual(100, report.policy_metrics["strong-only"]["p95_latency_ms"])
        self.assertEqual(200, report.policy_metrics["cheap-only"]["p95_latency_ms"])
        interval = report.candidate_vs_baseline["cheap-only"]["latency_regression_pct"]
        self.assertEqual(100.0, interval["mean_difference"])
        self.assertEqual(100.0, interval["upper"])
        gate = report.claim_gates["cheap-only"]
        self.assertFalse(gate["passed"])
        self.assertIn("latency_regression_failed", gate["reason_codes"])

    def test_proportional_case_latencies_keep_a_paired_p95_interval(self):
        baseline = [10] * 94 + [200] * 6
        report = self._run_latency_comparison(baseline, [ms // 2 for ms in baseline])
        interval = report.bootstrap["cheap-only"]["latency_regression_pct"]
        for key in ("mean_difference", "lower", "upper"):
            self.assertEqual(-50.0, interval[key])
        self.assertTrue(report.claim_gates["cheap-only"]["passed"])

    def test_zero_baseline_p95_makes_relative_regression_undefined(self):
        report = self._run_latency_comparison([0] * 99 + [100], [10] * 100)
        self.assertEqual(0, report.policy_metrics["strong-only"]["p95_latency_ms"])
        self.assertIsNone(report.candidate_vs_baseline["cheap-only"]["latency_regression_pct"])
        gate = report.claim_gates["cheap-only"]
        self.assertFalse(gate["passed"])
        self.assertIn("latency_incomplete", gate["reason_codes"])

    def test_missing_latency_suppresses_p95_comparison_even_with_zero_latency_weight(self):
        report = self._run_latency_comparison([100] * 30, [50] * 29 + [None], latency_weight=0)
        self.assertIsNone(report.candidate_vs_baseline["cheap-only"]["latency_regression_pct"])
        self.assertIsNone(report.bootstrap["cheap-only"]["latency_regression_pct"])
        gate = report.claim_gates["cheap-only"]
        self.assertFalse(gate["passed"])
        self.assertIn("latency_incomplete", gate["reason_codes"])

    def test_manifest_mismatch_partition_model_policy_and_baseline_fail_closed(self):
        with self.assertRaises(TournamentError):
            TournamentRunner().run(spec=self._spec(dataset_id="dataset_wrong"), manifest=self.manifest, records=self.records, policies=self._policies())
        with self.assertRaises(TournamentError):
            TournamentRunner().run(spec=self._spec(), manifest=self.manifest, records=self.records[:-1], policies=self._policies())
        with self.assertRaises(TournamentError):
            TournamentRunner().run(spec=self._spec(baseline_policy_id="missing"), manifest=self.manifest, records=self.records, policies=self._policies())
        with self.assertRaises(TournamentError):
            TournamentRunner().run(spec=self._spec(), manifest=self.manifest, records=self.records, policies=(FixedModelPolicy("not-present", policy_id="strong-only"),))

    def test_baseline_all_abstain_is_fatal_but_candidate_abstain_is_metric_coverage_loss(self):
        baseline = RecordedDecisionPolicy({}, policy_id="strong-only")
        with self.assertRaises(TournamentError):
            TournamentRunner().run(spec=self._spec(), manifest=self.manifest, records=self.records, policies=(baseline, FixedModelPolicy("seed-cheap", "cheap-only")))

    def test_duplicate_policy_ids_and_invalid_policy_decision_are_rejected(self):
        duplicate = (FixedModelPolicy("seed-strong", "strong-only"), FixedModelPolicy("seed-cheap", "strong-only"))
        with self.assertRaises(TournamentError):
            TournamentRunner().run(spec=self._spec(), manifest=self.manifest, records=self.records, policies=duplicate)


if __name__ == "__main__":
    unittest.main()
