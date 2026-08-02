import os
import tempfile
import time
import unittest
from unittest.mock import patch

from costweave.calibration import BetaPosterior, HierarchicalCalibrator
from costweave.capability_store import CapabilityStore
from costweave.control_kernel import ControlKernel
from costweave.dag_simulation import DagMonteCarlo, stable_seed
from costweave.domain import (
    Plan,
    RunRequest,
    RunStatus,
    TaskContract,
    TaskStatus,
)
from costweave.engine import RunManager
from costweave.evaluation import (
    brier_score,
    expected_calibration_error,
    interval_coverage,
    log_loss,
    run_fixed_benchmark,
)
from costweave.outcome_forecast import (
    OUTCOME_CATEGORIES,
    OutcomeCategory,
    OutcomeForecast,
    OutcomeRecord,
)


def task(
    task_id="a",
    *,
    dependencies=None,
    probability=0.84,
    lower=0.76,
    latency=1000,
    modality=None,
):
    return TaskContract(
        id=task_id,
        title=task_id,
        objective=f"complete {task_id}",
        task_type="analysis",
        dependencies=list(dependencies or []),
        required_capabilities=["analysis"],
        acceptance_criteria=["evidence is traceable"],
        output_schema=["summary", "evidence", "confidence"],
        predicted_success=probability,
        predicted_success_lower_bound=lower,
        route_confidence=0.82,
        classification_confidence=0.84,
        criticality=0.6,
        uncertainty=0.15,
        estimated_cost=0.1,
        estimated_latency_ms=latency,
        selected_worker="local-balanced",
        required_modalities=list(modality or ["text"]),
    )


def plan(tasks=None, confidence=0.84):
    return Plan(
        version=1,
        difficulty=5,
        task_types=["analysis"],
        rationale="test",
        tasks=list(tasks or [task()]),
        predicted_success=0.8,
        estimate_confidence=confidence,
        risks=[],
    )


class OutcomeContractTests(unittest.TestCase):
    def forecast(self, probabilities=None, cumulative=None):
        return OutcomeForecast.create(
            run_id="run_test",
            probabilities=probabilities or {
                OUTCOME_CATEGORIES[0]: 0.5,
                OUTCOME_CATEGORIES[1]: 0.2,
                OUTCOME_CATEGORIES[2]: 0.1,
                OUTCOME_CATEGORIES[3]: 0.1,
                OUTCOME_CATEGORIES[4]: 0.1,
            },
            cumulative_success=cumulative or {
                "first": 0.5,
                "repair": 0.7,
                "upgrade": 0.8,
                "human": 0.9,
            },
            quality={"p50": 0.8, "p95": 0.9},
            cost={"p50": 1.0, "p95": 1.5},
            completion_seconds={"p50": 2.0, "p95": 3.0},
            confidence=0.8,
            sample_size=1000,
            seed=7,
            out_of_distribution=False,
            ood_reasons=(),
            major_failure_modes=(),
            critical_false_pass_probability=0.01,
        )

    def test_all_exclusive_categories_are_required(self):
        values = {item: 0.2 for item in OUTCOME_CATEGORIES}
        values.pop(OUTCOME_CATEGORIES[-1])
        with self.assertRaises(ValueError):
            self.forecast(values)

    def test_probabilities_must_sum_to_one(self):
        with self.assertRaises(ValueError):
            self.forecast({item: 0.1 for item in OUTCOME_CATEGORIES})

    def test_cumulative_success_must_be_monotonic(self):
        with self.assertRaises(ValueError):
            self.forecast(
                cumulative={"first": 0.5, "repair": 0.4, "upgrade": 0.8}
            )

    def test_p95_cannot_be_lower_than_p50(self):
        forecast = self.forecast()
        values = forecast.to_dict()
        values["cost"] = {"p50": 2.0, "p95": 1.0}
        values.pop("forecast_id")
        values.pop("run_id")
        with self.assertRaises(ValueError):
            OutcomeForecast.create(run_id="run_test", **values)

    def test_forecast_identity_is_deterministic(self):
        self.assertEqual(
            self.forecast().forecast_id,
            self.forecast().forecast_id,
        )

    def test_outcome_rejects_incomplete_attribution(self):
        with self.assertRaises(ValueError):
            OutcomeRecord(
                "outcome_x",
                "run_x",
                OutcomeCategory.FIRST_PASS.value,
                True,
                0.8,
                1.0,
                2.0,
                False,
                {"model_ids": ["x"]},
                "2026-01-01T00:00:00Z",
            )

    def test_outcome_success_must_match_category(self):
        attribution = {
            "model_ids": ["x"],
            "prompt_version": "p1",
            "tool_version": "t1",
            "policy_revisions": {"routing": "r1"},
            "data_versions": {"catalog": "c1"},
            "plan_revision": 1,
        }
        with self.assertRaises(ValueError):
            OutcomeRecord(
                "outcome_x",
                "run_x",
                OutcomeCategory.FAILED.value,
                True,
                0,
                1,
                2,
                False,
                attribution,
                "2026-01-01T00:00:00Z",
            )


class DagSimulationTests(unittest.TestCase):
    def setUp(self):
        self.simulator = DagMonteCarlo()
        self.request = RunRequest("simulate outcome", budget=5).normalized()

    def test_fixed_seed_is_byte_for_byte_reproducible(self):
        first = self.simulator.simulate(
            run_id="run_fixed", plan=plan(), request=self.request, seed=42
        )
        second = self.simulator.simulate(
            run_id="run_fixed", plan=plan(), request=self.request, seed=42
        )
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_stable_seed_is_deterministic_and_sqlite_safe(self):
        seed = stable_seed("run_fixed")
        self.assertEqual(seed, stable_seed("run_fixed"))
        self.assertLess(seed, 1 << 63)

    def test_different_seed_changes_distribution(self):
        first = self.simulator.simulate(
            run_id="run_a", plan=plan(), request=self.request, seed=1
        )
        second = self.simulator.simulate(
            run_id="run_a", plan=plan(), request=self.request, seed=2
        )
        self.assertNotEqual(first.probabilities, second.probabilities)

    def test_mutually_exclusive_probabilities_sum_to_one(self):
        forecast = self.simulator.simulate(
            run_id="run_sum", plan=plan(), request=self.request
        )
        self.assertAlmostEqual(1.0, sum(forecast.probabilities.values()))

    def test_cumulative_success_is_monotonic(self):
        values = list(
            self.simulator.simulate(
                run_id="run_monotonic", plan=plan(), request=self.request
            ).cumulative_success.values()
        )
        self.assertEqual(values, sorted(values))

    def test_recovery_disabled_never_reports_repair_or_upgrade(self):
        request = RunRequest(
            "simulate disabled recovery", max_recoveries=0
        ).normalized()
        forecast = self.simulator.simulate(
            run_id="run_disabled",
            plan=plan([task(probability=0.2, lower=0.1)]),
            request=request,
        )
        self.assertEqual(
            0.0,
            forecast.probabilities[OutcomeCategory.REPAIRED.value],
        )
        self.assertEqual(
            0.0,
            forecast.probabilities[OutcomeCategory.UPGRADED.value],
        )

    def test_cost_and_time_intervals_are_ordered(self):
        forecast = self.simulator.simulate(
            run_id="run_interval", plan=plan(), request=self.request
        )
        self.assertGreaterEqual(forecast.cost["p95"], forecast.cost["p50"])
        self.assertGreaterEqual(
            forecast.completion_seconds["p95"],
            forecast.completion_seconds["p50"],
        )

    def test_non_text_modality_is_marked_ood(self):
        forecast = self.simulator.simulate(
            run_id="run_ood",
            plan=plan([task(modality=["video"])]),
            request=self.request,
        )
        self.assertTrue(forecast.out_of_distribution)
        self.assertIn("non_text_modality", forecast.ood_reasons)

    def test_low_confidence_is_marked_ood(self):
        forecast = self.simulator.simulate(
            run_id="run_low_confidence",
            plan=plan(confidence=0.4),
            request=self.request,
        )
        self.assertTrue(forecast.out_of_distribution)

    def test_parallel_dag_is_faster_than_serial_dag(self):
        parallel = plan(
            [task("a"), task("b"), task("c", dependencies=["a", "b"])]
        )
        serial = plan(
            [task("a"), task("b", dependencies=["a"]), task("c", dependencies=["b"])]
        )
        first = self.simulator.simulate(
            run_id="run_parallel", plan=parallel, request=self.request, seed=9
        )
        second = self.simulator.simulate(
            run_id="run_serial", plan=serial, request=self.request, seed=9
        )
        self.assertLess(
            first.completion_seconds["p50"],
            second.completion_seconds["p50"],
        )

    def test_sample_size_below_minimum_is_rejected(self):
        with self.assertRaises(ValueError):
            self.simulator.simulate(
                run_id="run_small",
                plan=plan(),
                request=self.request,
                sample_size=99,
            )

    def test_calibrated_probability_changes_future_forecast(self):
        baseline = self.simulator.simulate(
            run_id="run_calibrated",
            plan=plan(),
            request=self.request,
            seed=17,
        )
        calibrated = self.simulator.simulate(
            run_id="run_calibrated",
            plan=plan(),
            request=self.request,
            seed=17,
            calibrated_probabilities={"a": 0.98},
        )
        self.assertGreater(
            calibrated.probabilities[
                OutcomeCategory.FIRST_PASS.value
            ],
            baseline.probabilities[
                OutcomeCategory.FIRST_PASS.value
            ],
        )


class CalibrationTests(unittest.TestCase):
    def test_uniform_prior_mean_is_half(self):
        self.assertEqual(0.5, BetaPosterior().mean)

    def test_update_success_increases_mean(self):
        self.assertGreater(BetaPosterior().update(True).mean, 0.5)

    def test_update_failure_decreases_mean(self):
        self.assertLess(BetaPosterior().update(False).mean, 0.5)

    def test_update_rejects_nonpositive_weight(self):
        with self.assertRaises(ValueError):
            BetaPosterior().update(True, 0)

    def test_interval_contains_mean(self):
        posterior = BetaPosterior(80, 20)
        lower, upper = posterior.interval()
        self.assertLessEqual(lower, posterior.mean)
        self.assertGreaterEqual(upper, posterior.mean)

    def test_hierarchy_prefers_first_sufficient_specific_level(self):
        calibrator = HierarchicalCalibrator(minimum_samples=20)
        level, _, sparse = calibrator.choose(
            [
                ("exact", BetaPosterior(18, 4)),
                ("global", BetaPosterior(90, 12)),
            ]
        )
        self.assertEqual("exact", level)
        self.assertFalse(sparse)

    def test_sparse_hierarchy_falls_back_without_full_override(self):
        result = HierarchicalCalibrator(minimum_samples=20).calibrate(
            0.8, [("exact", BetaPosterior(2, 1))]
        )
        self.assertTrue(result["sparse"])
        self.assertGreater(result["probability"], 0.7)

    def test_dense_posterior_has_stronger_influence(self):
        calibrator = HierarchicalCalibrator(minimum_samples=20)
        sparse = calibrator.calibrate(
            0.5, [("exact", BetaPosterior(10, 2))]
        )
        dense = calibrator.calibrate(
            0.5, [("exact", BetaPosterior(100, 10))]
        )
        self.assertGreater(dense["probability"], sparse["probability"])

    def test_capability_store_updates_four_hierarchy_levels(self):
        with tempfile.TemporaryDirectory() as temp:
            path = os.path.join(temp, "runtime.sqlite3")
            ControlKernel(path)
            store = CapabilityStore(path)
            store.update(
                model_id="model-a",
                task_type="analysis",
                risk_level="normal",
                success=True,
            )
            self.assertEqual(4, len(store.list()))
            self.assertEqual(
                ["exact", "model_task", "model", "global"],
                [item[0] for item in store.candidates(
                    model_id="model-a",
                    task_type="analysis",
                    risk_level="normal",
                )],
            )


class EvaluationTests(unittest.TestCase):
    def test_perfect_brier_is_zero(self):
        self.assertEqual(0.0, brier_score([0, 1], [0, 1]))

    def test_log_loss_rewards_better_predictions(self):
        self.assertLess(
            log_loss([0.1, 0.9], [0, 1]),
            log_loss([0.4, 0.6], [0, 1]),
        )

    def test_ece_is_zero_for_exact_bucket_frequencies(self):
        predictions = [0.25] * 4 + [0.75] * 4
        outcomes = [1, 0, 0, 0, 1, 1, 1, 0]
        self.assertEqual(
            0.0,
            expected_calibration_error(
                predictions, outcomes, bins=4
            ),
        )

    def test_interval_coverage(self):
        self.assertEqual(
            0.5,
            interval_coverage([1, 5], [0, 0], [2, 4]),
        )

    def test_metrics_reject_empty_input(self):
        for function in (
            brier_score,
            log_loss,
            expected_calibration_error,
        ):
            with self.assertRaises(ValueError):
                function([], [])

    def test_fixed_benchmark_meets_ece_target(self):
        result = run_fixed_benchmark()
        self.assertLessEqual(
            result["metrics"]["costweave_v0.4.7"]["ece"], 0.05
        )

    def test_fixed_benchmark_beats_v03_brier(self):
        result = run_fixed_benchmark()
        self.assertLess(
            result["metrics"]["costweave_v0.4.7"]["brier"],
            result["metrics"]["v0.3_uncalibrated"]["brier"],
        )

    def test_fixed_benchmark_interval_coverage_is_in_target(self):
        result = run_fixed_benchmark()
        self.assertTrue(0.9 <= result["cost_interval_coverage"] <= 0.99)
        self.assertTrue(0.9 <= result["time_interval_coverage"] <= 0.99)

    def test_fixed_benchmark_rejects_small_buckets(self):
        with self.assertRaises(ValueError):
            run_fixed_benchmark(199)


class OutcomeIntegrationTests(unittest.TestCase):
    def test_persistent_run_records_forecast_outcome_and_posteriors(self):
        with tempfile.TemporaryDirectory() as temp:
            manager = RunManager(
                runtime_db_path=os.path.join(temp, "runtime.sqlite3"),
                recover=False,
            )
            record = manager.create(
                RunRequest("分析一个产品方案", budget=3),
                idempotency_key="forecast-integration-0001",
            )
            for _ in range(200):
                if record.status in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }:
                    break
                time.sleep(0.05)
            manager.shutdown()
            self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
            forecast = manager.forecast(record.id)
            self.assertIsNotNone(forecast)
            self.assertEqual(1200, forecast["sample_size"])
            health = manager.health()
            self.assertEqual(1, health["forecast_snapshots"])
            self.assertEqual(1, health["outcomes"])
            self.assertGreater(health["capability_posteriors"], 0)
            self.assertGreater(health["forecast_calibrators"], 0)

    def test_feature_flag_disables_new_forecast_writes(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict(
            os.environ, {"COSTWEAVE_OUTCOME_FORECAST": "0"}
        ):
            manager = RunManager(
                runtime_db_path=os.path.join(temp, "runtime.sqlite3"),
                recover=False,
            )
            record = manager.create(
                RunRequest("分析一个产品方案", budget=3),
                idempotency_key="forecast-disabled-0001",
            )
            for _ in range(200):
                if record.status in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                }:
                    break
                time.sleep(0.05)
            manager.shutdown()
            self.assertIsNone(manager.forecast(record.id))
            self.assertEqual(0, manager.health()["outcomes"])

    def test_evaluation_is_persisted_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            kernel = ControlKernel(os.path.join(temp, "runtime.sqlite3"))
            first = kernel.evaluations()
            second = kernel.evaluations()
            self.assertEqual(first, second)
            self.assertTrue(first[0]["passed"])
            self.assertEqual(1, kernel.health()["evaluation_runs"])


if __name__ == "__main__":
    unittest.main()
