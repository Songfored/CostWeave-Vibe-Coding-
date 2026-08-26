"""Metrics, paired bootstrap, and claim-gate tests for v0.6.0."""

from __future__ import annotations

import hashlib
import unittest

from costweave.benchmarks.claims import evaluate_claim_gate
from costweave.benchmarks.contracts import RoutingRecordV1
from costweave.benchmarks.metrics import evaluate_policy_decisions, summarize_policy
from costweave.benchmarks.policies import FixedModelPolicy
from costweave.benchmarks.statistics import (
    BootstrapInterval,
    paired_bootstrap_cost_reduction_pct,
    paired_bootstrap_difference,
)
from costweave.benchmarks.contracts import CaseViewV1


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _records() -> tuple[RoutingRecordV1, ...]:
    rows = (
        ("case-1", "model-a", 0.90, True, 0.010, 100),
        ("case-1", "model-b", 0.80, True, 0.004, 80),
        ("case-2", "model-a", 0.60, False, 0.010, 100),
        ("case-2", "model-b", 0.85, True, 0.004, 80),
        ("case-3", "model-a", 0.70, True, 0.010, None),
        ("case-3", "model-b", 0.65, True, 0.004, None),
    )
    return tuple(
        RoutingRecordV1(
            source_record_id=f"{case}-{model}",
            case_id=case,
            partition="test",
            task_family="code",
            prompt_hash=_hash(case),
            model_id=model,
            response_hash=_hash(f"{case}-{model}"),
            quality_score=quality,
            accepted=accepted,
            input_tokens=10,
            output_tokens=20,
            cost_usd=cost,
            latency_ms=latency,
            source_dataset="fixture",
            source_version="1",
            price_snapshot_id="prices-1",
            evidence_level="E1-public-replay",
            observed_at="2026-08-09T12:00:00Z",
        )
        for case, model, quality, accepted, cost, latency in rows
    )


def _cases() -> tuple[CaseViewV1, ...]:
    return tuple(
        CaseViewV1(case, "code", _hash(case), ("model-a", "model-b"))
        for case in ("case-1", "case-2", "case-3")
    )


class MetricsV060Tests(unittest.TestCase):
    def test_evaluation_joins_only_after_blind_decision_and_summarizes(self):
        policy = FixedModelPolicy("model-a", policy_id="candidate")
        decisions = tuple(policy.decide(case) for case in _cases())
        outcomes = evaluate_policy_decisions(
            _records(), decisions, quality_weight=1.0, cost_weight=0.0, latency_weight=0.0
        )
        metrics = summarize_policy(outcomes)
        self.assertEqual(3, metrics.total_cases)
        self.assertEqual(3, metrics.decided_cases)
        self.assertEqual(2, metrics.accepted_count)
        self.assertAlmostEqual(2 / 3, metrics.accepted_rate)
        self.assertEqual({"model-a": 3}, metrics.model_selection_counts)
        self.assertEqual(1, metrics.failure_case_total_count)
        self.assertEqual(("case-2",), metrics.failure_case_ids)

    def test_abstain_is_coverage_loss_and_does_not_expose_result(self):
        policy = FixedModelPolicy("missing", policy_id="abstain")
        outcomes = evaluate_policy_decisions(
            _records(), tuple(policy.decide(case) for case in _cases()), quality_weight=1.0, cost_weight=0.0, latency_weight=0.0
        )
        metrics = summarize_policy(outcomes)
        self.assertEqual(0, metrics.decided_cases)
        self.assertEqual(0.0, metrics.coverage)
        self.assertIsNone(metrics.accepted_rate)
        self.assertEqual({}, metrics.model_selection_counts)

    def test_latency_weight_requires_latency_and_zero_weight_allows_missing(self):
        decisions = tuple(FixedModelPolicy("model-a", policy_id="candidate").decide(case) for case in _cases())
        with self.assertRaises(ValueError):
            evaluate_policy_decisions(_records(), decisions, quality_weight=1.0, cost_weight=0.0, latency_weight=1.0)
        outcomes = evaluate_policy_decisions(_records(), decisions, quality_weight=1.0, cost_weight=0.0, latency_weight=0.0)
        self.assertIsNotNone(summarize_policy(outcomes).p95_latency_ms)

    def test_invalid_decision_case_or_mixed_policy_is_rejected(self):
        policy_a = FixedModelPolicy("model-a", policy_id="a")
        policy_b = FixedModelPolicy("model-b", policy_id="b")
        with self.assertRaises(ValueError):
            evaluate_policy_decisions(_records(), (policy_a.decide(CaseViewV1("case-unknown", "code", _hash("case-unknown"), ("model-a", "model-b"))),), quality_weight=1, cost_weight=0, latency_weight=0)
        with self.assertRaises(ValueError):
            evaluate_policy_decisions(_records(), (policy_a.decide(_cases()[0]), policy_b.decide(_cases()[1])), quality_weight=1, cost_weight=0, latency_weight=0)


class StatisticsV060Tests(unittest.TestCase):
    def test_paired_bootstrap_is_deterministic_and_input_preserving(self):
        candidate = [1.0, 2.0, 3.0]
        baseline = [0.0, 1.0, 2.0]
        first = paired_bootstrap_difference(candidate, baseline, samples=100, seed=7)
        second = paired_bootstrap_difference(candidate, baseline, samples=100, seed=7)
        self.assertEqual(first, second)
        self.assertEqual([1.0, 2.0, 3.0], candidate)
        self.assertEqual([0.0, 1.0, 2.0], baseline)
        self.assertAlmostEqual(1.0, first.mean_difference)

    def test_paired_bootstrap_rejects_invalid_shapes_values_and_seed(self):
        with self.assertRaises(ValueError):
            paired_bootstrap_difference([1], [1, 2], samples=10, seed=1)
        with self.assertRaises(ValueError):
            paired_bootstrap_difference([float("nan")], [1], samples=10, seed=1)
        with self.assertRaises(ValueError):
            paired_bootstrap_difference([1], [1], samples=0, seed=1)
        with self.assertRaises(ValueError):
            paired_bootstrap_difference([1], [1], samples=10, seed=2**31)

    def test_cost_reduction_handles_zero_baseline_without_infinity(self):
        self.assertIsNone(paired_bootstrap_cost_reduction_pct([0, 1], [0, 0], samples=100, seed=1))
        interval = paired_bootstrap_cost_reduction_pct([1, 1], [2, 2], samples=100, seed=1)
        self.assertIsNotNone(interval)
        self.assertAlmostEqual(50.0, interval.mean_difference)


class ClaimsV060Tests(unittest.TestCase):
    @staticmethod
    def _metrics(policy_id: str):
        from costweave.benchmarks.metrics import PolicyMetricsV1

        return PolicyMetricsV1(
            policy_id=policy_id,
            total_cases=40,
            decided_cases=40,
            coverage=1,
            accepted_count=38,
            accepted_rate=0.95,
            mean_quality=0.9,
            total_cost_usd=10,
            cost_per_accepted_result_usd=10 / 38,
            p50_latency_ms=100,
            p95_latency_ms=150,
            mean_utility=0.8,
            error_risk=0.05,
            oracle_agreement=0.9,
            mean_decision_regret=0.1,
            model_selection_counts={"model-a": 40},
            failure_case_ids=(),
            failure_case_total_count=2,
        )

    @staticmethod
    def _interval(mean: float, lower: float, upper: float) -> BootstrapInterval:
        return BootstrapInterval(mean, lower, upper, 1000, 3)

    def test_noninferior_cost_reduction_path_passes(self):
        result = evaluate_claim_gate(
            candidate_metrics=self._metrics("candidate"), baseline_metrics=self._metrics("baseline"),
            quality_difference_pp=self._interval(1, 0, 2), cost_reduction_pct=self._interval(20, 12, 30),
            latency_regression_pct=self._interval(1, -1, 5), case_count=40, evidence_level="E1-public-replay", partition="test",
        )
        self.assertTrue(result.passed)
        self.assertEqual("claimable", result.status)

    def test_quality_gain_path_passes_when_cost_is_not_worse(self):
        result = evaluate_claim_gate(
            candidate_metrics=self._metrics("candidate"), baseline_metrics=self._metrics("baseline"),
            quality_difference_pp=self._interval(4, 3.2, 6), cost_reduction_pct=self._interval(0, 0, 2),
            latency_regression_pct=self._interval(1, -1, 5), case_count=40, evidence_level="E2-directional-real", partition="test",
        )
        self.assertTrue(result.passed)
        self.assertEqual("quality_improvement_without_cost_regression", result.path)

    def test_seed_is_insufficient_sample_and_e0_never_claimable(self):
        result = evaluate_claim_gate(
            candidate_metrics=self._metrics("candidate"), baseline_metrics=self._metrics("baseline"),
            quality_difference_pp=self._interval(10, 9, 11), cost_reduction_pct=self._interval(50, 40, 60),
            latency_regression_pct=self._interval(0, -1, 1), case_count=12, evidence_level="E0-simulated", partition="test",
        )
        self.assertFalse(result.passed)
        self.assertEqual("insufficient_sample", result.status)
        self.assertIn("evidence_below_e1", result.reason_codes)

    def test_claim_guardrails_reject_oracle_partition_latency_and_blindness(self):
        result = evaluate_claim_gate(
            candidate_metrics=self._metrics("same"), baseline_metrics=self._metrics("same"),
            quality_difference_pp=self._interval(1, 0, 2), cost_reduction_pct=self._interval(20, 12, 30),
            latency_regression_pct=self._interval(1, -1, 5), case_count=40, evidence_level="E1-public-replay", partition="train",
            candidate_is_oracle=True, blind_fields_exposed=True,
        )
        self.assertFalse(result.passed)
        self.assertEqual("not_supported", result.status)
        self.assertIn("candidate_equals_baseline", result.reason_codes)


if __name__ == "__main__":
    unittest.main()
