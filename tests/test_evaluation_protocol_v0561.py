from __future__ import annotations

import csv
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from costweave.baseline_runner import (
    BaselineRunner,
    ExecutionOutcome,
    build_seed_runner,
    export_report,
)
from costweave.evaluation_protocol import (
    BASELINE_CHEAPEST,
    BASELINE_IDS,
    BASELINE_STRONGEST,
    EVALUATION_PROTOCOL_VERSION,
    SIMULATED_EVIDENCE_LEVEL,
    BenchmarkTask,
    EvaluationLimits,
    EvaluationProtocolError,
    ExperimentSpec,
    build_default_experiment_spec,
    load_seed_tasks,
    validate_task_set,
)
from costweave.evaluation_store import EvaluationProtocolStore
from costweave.event_store import EventStore
from costweave.model_catalog import MODELS


class EvaluationProtocolContractTests(unittest.TestCase):
    def setUp(self):
        self.tasks = load_seed_tasks()
        self.runner = build_seed_runner()

    def test_seed_set_has_35_tasks_and_required_coverage(self):
        self.assertEqual(35, len(self.tasks))
        task_types = {
            item.task_contract.task_type for item in self.tasks
        }
        self.assertTrue({
            "coding",
            "research",
            "writing",
            "analysis",
            "long_context",
            "translation",
            "vision",
        }.issubset(task_types))
        self.assertTrue(any(
            item.task_contract.requires_tools for item in self.tasks
        ))

    def test_every_task_has_preregistered_success_contract(self):
        for item in self.tasks:
            self.assertEqual(
                item.benchmark_id, item.success_contract.task_id
            )
            self.assertGreater(len(item.success_contract.criteria), 0)
            self.assertEqual(
                item.success_contract.to_dict(),
                item.task_contract.success_contract,
            )

    def test_project_partitions_are_isolated(self):
        projects = {}
        for item in self.tasks:
            projects.setdefault(item.project_id, set()).add(
                item.partition
            )
        self.assertTrue(all(len(value) == 1 for value in projects.values()))

    def test_project_partition_leakage_is_rejected(self):
        leaked = BenchmarkTask.from_dict(self.tasks[0].to_dict())
        leaked.benchmark_id = "benchmark_leaked_copy"
        leaked.task_contract.id = leaked.benchmark_id
        leaked.success_contract = replace(
            leaked.success_contract,
            task_id=leaked.benchmark_id,
        )
        leaked.partition = "test"
        with self.assertRaises(EvaluationProtocolError):
            validate_task_set((*self.tasks, leaked))

    def test_task_contract_round_trips(self):
        original = self.tasks[0]
        restored = BenchmarkTask.from_dict(original.to_dict())
        self.assertEqual(original.to_dict(), restored.to_dict())

    def test_task_order_is_deterministic_and_seed_sensitive(self):
        ids = [item.benchmark_id for item in self.tasks]
        first = self.runner.spec.ordered_task_ids(ids)
        second = self.runner.spec.ordered_task_ids(reversed(ids))
        self.assertEqual(first, second)
        changed = replace(
            self.runner.spec,
            experiment_id="experiment_changed_seed",
            random_seed=self.runner.spec.random_seed + 1,
        )
        self.assertNotEqual(first, changed.ordered_task_ids(ids))

    def test_exactly_five_policy_revisions_are_required(self):
        payload = self.runner.spec.to_dict()
        payload["policy_versions"].pop(BASELINE_CHEAPEST)
        with self.assertRaises(EvaluationProtocolError):
            ExperimentSpec.from_dict(payload)

    def test_real_provider_calls_cannot_be_enabled(self):
        payload = self.runner.spec.to_dict()
        payload["real_provider_calls_allowed"] = True
        with self.assertRaises(EvaluationProtocolError):
            ExperimentSpec.from_dict(payload)

    def test_spec_round_trips_without_losing_fingerprint(self):
        restored = ExperimentSpec.from_dict(
            self.runner.spec.to_dict()
        )
        self.assertEqual(
            self.runner.spec.fingerprint, restored.fingerprint
        )


class SoftOnlyBackend:
    revision = "soft-only-test"
    execution_mode = "deterministic_fixture_simulation"
    evidence_level = SIMULATED_EVIDENCE_LEVEL

    def execute(
        self,
        *,
        task,
        success_contract,
        route,
        decision,
        seed,
        **limits,
    ):
        return ExecutionOutcome(
            status="evaluated",
            accepted=False,
            hard_pass=False,
            soft_quality=1.0,
            criteria_results=tuple(
                {
                    "criterion_id": item.criterion_id,
                    "critical": item.critical,
                    "passed": False if item.critical else True,
                    "evidence_ref": "fixture://soft-only",
                }
                for item in success_contract.criteria
            ),
            output={
                "summary": "高软评分但关键条件失败。",
                "deliverable": "fixture",
                "evidence": ["fixture://soft-only"],
                "confidence": 1.0,
            },
            cost_breakdown_usd={
                "execution": 0.0,
                "retry": 0.0,
                "validation": 0.0,
                "tools": 0.0,
            },
            duration_breakdown_ms={
                "execution": 1,
                "validation": 1,
                "tools": 0,
            },
            attempts=1,
            simulated_calls=2,
            tool_calls=0,
            provider_calls=0,
            evidence_level=self.evidence_level,
            backend_revision=self.revision,
        )


def all_mapping_keys(value):
    keys = set()
    if isinstance(value, dict):
        for key, item in value.items():
            keys.add(key)
            keys.update(all_mapping_keys(item))
    elif isinstance(value, list):
        for item in value:
            keys.update(all_mapping_keys(item))
    return keys


class BaselineRunnerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = build_seed_runner()
        cls.report = cls.runner.run()

    def test_report_contains_complete_five_by_35_matrix(self):
        self.assertEqual(BASELINE_IDS, tuple(
            item.baseline_id for item in self.report.baselines
        ))
        self.assertEqual(175, len(self.report.observations))
        self.assertTrue(self.report.passed)

    def test_deterministic_fixture_is_reproducible(self):
        second = build_seed_runner().run()
        self.assertEqual(self.report.report_id, second.report_id)
        self.assertEqual(
            [item.observation_id for item in self.report.observations],
            [item.observation_id for item in second.observations],
        )

    def test_same_model_and_task_produce_same_fixture_result(self):
        groups = {}
        for item in self.report.observations:
            key = (item.task_id, item.route.model_id)
            groups.setdefault(key, []).append(item)
        repeated = [items for items in groups.values() if len(items) > 1]
        self.assertTrue(repeated)
        for items in repeated:
            first = items[0].outcome
            for item in items[1:]:
                self.assertEqual(first.accepted, item.outcome.accepted)
                self.assertEqual(
                    first.soft_quality, item.outcome.soft_quality
                )

    def test_strongest_and_cheapest_apply_their_registered_objectives(self):
        by_task = {}
        for item in self.report.observations:
            by_task.setdefault(item.task_id, {})[
                item.baseline_id
            ] = item
        for policies in by_task.values():
            strongest = policies[BASELINE_STRONGEST].route
            cheapest = policies[BASELINE_CHEAPEST].route
            self.assertGreaterEqual(
                strongest.success_lower_bound,
                cheapest.success_lower_bound,
            )
            self.assertLessEqual(
                cheapest.estimated_cost_usd,
                strongest.estimated_cost_usd,
            )

    def test_blind_packets_contain_no_route_identity_or_cost(self):
        forbidden = {
            "baseline_id",
            "model_id",
            "model_name",
            "provider",
            "policy_revision",
            "price",
            "cost",
            "route",
        }
        for packet in self.report.blind_review_packets:
            self.assertTrue(packet["identity_hidden"])
            self.assertIsNone(
                packet["hard_review"]["review_result"]
            )
            self.assertIsNone(packet["soft_review"]["score"])
            self.assertTrue(
                forbidden.isdisjoint(all_mapping_keys(packet))
            )

    def test_execution_view_cannot_contain_reference_answer(self):
        for task in self.report.tasks:
            payload = task.execution_view().to_dict()
            self.assertNotIn("reference_answer", payload)
            self.assertNotIn("independent_validator", payload)
            self.assertNotIn("partition", payload)

    def test_hard_failure_cannot_be_overridden_by_soft_score(self):
        runner = BaselineRunner(
            spec=self.runner.spec,
            tasks=self.runner.tasks,
            workers=MODELS,
            backend=SoftOnlyBackend(),
        )
        report = runner.run()
        self.assertTrue(report.passed)
        self.assertTrue(all(
            item.outcome.soft_quality == 1.0
            and not item.outcome.hard_pass
            and not item.outcome.accepted
            for item in report.observations
        ))

    def test_provider_calls_and_commercial_claims_are_disabled(self):
        self.assertEqual(0, self.report.provider_call_count)
        self.assertFalse(self.report.commercial_claims_allowed)
        self.assertEqual(
            SIMULATED_EVIDENCE_LEVEL, self.report.evidence_level
        )

    def test_low_shared_call_limit_rejects_without_exceeding(self):
        limited_spec = replace(
            self.runner.spec,
            experiment_id="experiment_low_call_limit",
            limits=EvaluationLimits(
                max_cost_usd=25,
                max_calls=2,
                max_wall_seconds=3600,
                max_tool_calls=100,
            ),
        )
        report = BaselineRunner(
            spec=limited_spec,
            tasks=self.runner.tasks,
            workers=MODELS,
        ).run()
        for metrics in report.metrics.values():
            self.assertLessEqual(metrics["simulated_calls"], 2)
            self.assertLessEqual(metrics["evaluated"], 1)

    def test_unroutable_task_is_recorded_instead_of_aborting_matrix(self):
        task = BenchmarkTask.from_dict(
            self.runner.tasks[0].to_dict()
        )
        task.task_contract.required_capabilities = [
            "impossible-capability"
        ]
        report = BaselineRunner(
            spec=self.runner.spec,
            tasks=[task],
            workers=MODELS,
        ).run()
        self.assertEqual(5, len(report.observations))
        self.assertTrue(all(
            item.outcome.status == "route_rejected"
            and item.route is None
            for item in report.observations
        ))
        self.assertTrue(report.passed)

    def test_cost_breakdowns_reconcile(self):
        for item in self.report.observations:
            self.assertAlmostEqual(
                item.outcome.total_cost_usd,
                sum(item.outcome.cost_breakdown_usd.values()),
            )
        self.assertIn(
            "cost_per_accepted_task_usd",
            self.report.metrics[BASELINE_STRONGEST],
        )
        self.assertIn(
            "routing_regret",
            self.report.metrics[BASELINE_STRONGEST],
        )

    def test_jsonl_and_csv_exports_are_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            artifacts = export_report(self.report, directory)
            observations = Path(
                artifacts["observations_jsonl"]
            ).read_text(encoding="utf-8").splitlines()
            tasks = Path(
                artifacts["tasks_jsonl"]
            ).read_text(encoding="utf-8").splitlines()
            blind = Path(
                artifacts["blind_review_jsonl"]
            ).read_text(encoding="utf-8").splitlines()
            with Path(artifacts["summary_csv"]).open(
                encoding="utf-8", newline=""
            ) as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(175, len(observations))
            self.assertEqual(35, len(tasks))
            self.assertEqual(175, len(blind))
            self.assertEqual(5, len(rows))
            self.assertEqual(
                self.report.report_id,
                json.loads(
                    Path(artifacts["report"]).read_text(
                        encoding="utf-8"
                    )
                )["report_id"],
            )
            self.assertEqual(
                artifacts, export_report(self.report, directory)
            )
            with self.assertRaises(EvaluationProtocolError):
                export_report(
                    build_seed_runner(random_seed=5602).run(),
                    directory,
                )


class EvaluationProtocolStoreTests(unittest.TestCase):
    def test_migration_and_idempotent_report_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "runtime.sqlite3"
            health = EventStore(database).health()
            self.assertEqual("v6.1.0", health["runtime_schema"])
            self.assertEqual(31, health["migrations"])
            report = build_seed_runner().run()
            store = EvaluationProtocolStore(database)
            self.assertTrue(store.record(report))
            self.assertFalse(store.record(report))
            self.assertEqual(report.report_id, store.get(
                report.report_id
            )["report_id"])
            self.assertEqual(1, len(store.list()))
            self.assertEqual(
                175, len(store.observations(report.report_id))
            )
            self.assertEqual(
                {
                    "protocol_evaluation_runs": 1,
                    "evaluation_observations": 175,
                },
                store.counts(),
            )

    def test_default_spec_marks_protocol_revision(self):
        spec = build_default_experiment_spec(
            catalog_revision="fixture",
            catalog_hash="abc123",
        )
        self.assertEqual(EVALUATION_PROTOCOL_VERSION, spec.schema_version)


if __name__ == "__main__":
    unittest.main()
