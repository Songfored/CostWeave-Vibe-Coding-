import tempfile
import time
import unittest

from costweave.adaptive_recovery import (
    CompletionEstimator,
    PollutionAnalyzer,
    RecoveryGate,
)
from costweave.artifacts import Artifact, ArtifactStatus
from costweave.domain import (
    Plan,
    RunRequest,
    RunStatus,
    TaskContract,
    TaskStatus,
    WorkerProfile,
)
from costweave.engine import RunManager, RunRecord
from costweave.failure_classifier import FailureRecord
from costweave.graph_compiler import GraphCompiler
from costweave.handoff_compiler import (
    HANDOFF_SCHEMA_VERSION,
    HandoffCompiler,
    HandoffPreflightError,
)
from costweave.messages import MessageEnvelope
from costweave.planner import HeuristicPlanner
from costweave.projections import canonical_task_id
from costweave.recovery import RecoveryAction, RecoveryDecision
from costweave.success_contract import SuccessContract


def worker(**updates):
    values = {
        "id": "receiver",
        "name": "Receiver",
        "specialty": "analysis",
        "capabilities": {"analysis": 0.9},
        "cost_per_task": 0.01,
        "latency_factor": 1.0,
        "reliability": 0.9,
        "context_window": 32_000,
        "max_output_tokens": 8_000,
        "modalities": ["text"],
        "tools": ["web_search"],
    }
    values.update(updates)
    return WorkerProfile(**values)


def task(task_id, *, dependencies=None, dependency_types=None):
    return TaskContract(
        id=task_id,
        title=task_id,
        objective=f"complete {task_id}",
        task_type="analysis",
        dependencies=list(dependencies or []),
        dependency_types=dict(dependency_types or {}),
        required_capabilities=["analysis"],
        acceptance_criteria=["result is evidence-backed"],
        output_schema=["summary", "evidence", "confidence"],
        output_artifact_specs=[
            {
                "name": "primary",
                "artifact_type": "analysis_result",
                "schema_ref": "artifact://analysis/1",
                "sensitivity": "internal",
            }
        ],
        include=["declared scope"],
        exclude=["scope expansion"],
        predicted_success=0.86,
        predicted_success_lower_bound=0.78,
        estimated_input_tokens=2_000,
        estimated_output_tokens=800,
        estimated_cost=0.01,
        estimated_latency_ms=100,
        plan_version=2,
    )


def plan(tasks):
    return Plan(
        version=2,
        difficulty=5,
        task_types=["analysis"],
        rationale="test",
        tasks=tasks,
        predicted_success=0.8,
        estimate_confidence=0.8,
        risks=[],
    )


class HandoffCompilerTests(unittest.TestCase):
    def setUp(self):
        self.compiler = HandoffCompiler()
        self.run_id = "run_" + "1" * 32
        self.producer = task("producer")
        self.consumer = task(
            "consumer",
            dependencies=["producer"],
            dependency_types={"producer": "hard"},
        )
        self.consumer.requires_tools = ["web_search"]
        self.plan = plan([self.producer, self.consumer])
        for item in self.plan.tasks:
            item.success_contract = SuccessContract.from_task(
                item
            ).to_dict()
        self.artifact = Artifact.create(
            run_id=self.run_id,
            producer_task_id=canonical_task_id(
                self.run_id, self.producer.id
            ),
            artifact_type="analysis_result",
            schema_ref="artifact://analysis/1",
            content={
                "summary": "private body must not enter the prompt",
                "evidence": ["test"],
                "confidence": 0.9,
            },
            plan_revision=2,
            status=ArtifactStatus.VALIDATED,
            producer_model="producer",
            input_artifact_refs=[],
            attempt=1,
        )

    def compile(self, **updates):
        values = {
            "run_id": self.run_id,
            "goal": "build a verified report",
            "plan": self.plan,
            "task": self.consumer,
            "artifacts": [self.artifact],
            "worker": worker(),
            "remaining_budget": 1.0,
        }
        values.update(updates)
        return self.compiler.compile(**values)

    def test_compiles_minimum_context_and_does_not_copy_artifact_body(self):
        compiled = self.compile()
        self.assertTrue(compiled.preflight.admitted)
        self.assertEqual(
            HANDOFF_SCHEMA_VERSION,
            compiled.preflight.schema_version,
        )
        self.assertNotIn(
            "private body must not enter the prompt",
            compiled.compiled_prompt,
        )
        artifact_entry = next(
            item
            for item in compiled.context_manifest
            if item["kind"] == "artifact"
        )
        self.assertFalse(artifact_entry["content_embedded"])
        self.assertEqual(
            self.artifact.content_hash,
            artifact_entry["content_hash"],
        )
        self.assertEqual(
            ["web_search"],
            list(compiled.work_order.allowed_tools),
        )

    def test_receiver_missing_tool_is_rejected_before_execution(self):
        with self.assertRaises(HandoffPreflightError) as raised:
            self.compile(worker=worker(tools=[]))
        failures = {
            item.name for item in raised.exception.receipt.failures
        }
        self.assertIn("receiver_tools", failures)

    def test_schema_mismatch_is_rejected(self):
        self.consumer.input_artifact_schemas[
            "producer"
        ] = "artifact://analysis/2"
        with self.assertRaises(HandoffPreflightError) as raised:
            self.compile()
        self.assertIn(
            "artifact_integrity_schema_permission",
            {item.name for item in raised.exception.receipt.failures},
        )

    def test_sensitive_artifact_outside_permission_is_rejected(self):
        sensitive = Artifact.create(
            run_id=self.run_id,
            producer_task_id=self.artifact.producer_task_id,
            artifact_type=self.artifact.artifact_type,
            schema_ref=self.artifact.schema_ref,
            content=self.artifact.content,
            plan_revision=2,
            status=ArtifactStatus.VALIDATED,
            producer_model="producer",
            input_artifact_refs=[],
            attempt=1,
            sensitivity="sensitive",
        )
        with self.assertRaises(HandoffPreflightError):
            self.compile(artifacts=[sensitive])

    def test_legacy_message_envelope_remains_readable(self):
        modern = MessageEnvelope.create(
            run_id=self.run_id,
            recipient_task_id=canonical_task_id(
                self.run_id, self.consumer.id
            ),
            plan_revision=2,
            attempt=1,
            goal="goal",
            objective="objective",
            input_artifact_refs=[],
            output_artifact_specs=self.consumer.output_artifact_specs,
            acceptance_criteria=self.consumer.acceptance_criteria,
            include=[],
            exclude=[],
        ).to_dict()
        for key in (
            "context_manifest",
            "work_order",
            "preflight",
            "contract_digest",
            "compiled_prompt",
            "compiler_revision",
        ):
            modern.pop(key)
        modern["schema_version"] = "v4.3"
        restored = MessageEnvelope.from_dict(modern)
        self.assertEqual("v4.3", restored.schema_version)
        self.assertEqual({}, restored.work_order)


class AdaptiveRecoveryTests(unittest.TestCase):
    def test_graph_compiler_synchronizes_task_plan_version(self):
        first = task("first")
        second = task("second", dependencies=["first"])
        first.plan_version = second.plan_version = 1
        graph = GraphCompiler().compile("run_" + "2" * 32, plan([first, second]))
        self.assertEqual(2, graph.plan_revision)
        self.assertEqual([2, 2], [first.plan_version, second.plan_version])

    def test_pcomplete_uses_high_risk_threshold(self):
        item = task("high-risk")
        item.risk_level = "high"
        item.success_contract = SuccessContract.from_task(
            item
        ).to_dict()
        record = RunRecord(
            id="run_" + "3" * 32,
            request=RunRequest("high risk goal"),
            plan=plan([item]),
        )
        checkpoint = CompletionEstimator().estimate(
            record, stage="test"
        )
        self.assertEqual(0.90, checkpoint.threshold)
        self.assertEqual("high", checkpoint.risk_band)
        self.assertIn("remaining_critical_path", checkpoint.signals)

    def test_pollution_preserves_unrelated_sibling(self):
        root = task("root")
        failed = task("failed", dependencies=["root"])
        child = task(
            "child",
            dependencies=["failed"],
            dependency_types={"failed": "validation"},
        )
        sibling = task("sibling", dependencies=["root"])
        report = PollutionAnalyzer().analyze(
            plan([root, failed, child, sibling]),
            failed_task_id="failed",
            requested_scope="subgraph",
            failure_class="contract_error",
        )
        self.assertEqual(
            ("child", "failed"),
            report.invalidated_task_ids,
        )
        self.assertIn("sibling", report.preserved_task_ids)

    def test_soft_dependency_becomes_suspect_not_invalidated(self):
        failed = task("failed")
        soft = task(
            "soft",
            dependencies=["failed"],
            dependency_types={"failed": "soft"},
        )
        report = PollutionAnalyzer().analyze(
            plan([failed, soft]),
            failed_task_id="failed",
            requested_scope="subgraph",
            failure_class="evidence_failure",
        )
        self.assertEqual(("failed",), report.invalidated_task_ids)
        self.assertEqual(("soft",), report.suspect_task_ids)

    def test_second_global_replan_is_rejected(self):
        item = task("failed")
        record = RunRecord(
            id="run_" + "4" * 32,
            request=RunRequest("goal"),
            plan=plan([item]),
            global_replans=1,
        )
        failure = FailureRecord(
            failure_id="failure_" + "1" * 32,
            task_id=item.id,
            attempt=1,
            classification="root_assumption_error",
            confidence=0.94,
            signals=("root-assumption-signal",),
            source="validation",
            created_at="2026-01-01T00:00:00Z",
        )
        decision = RecoveryDecision(
            action=RecoveryAction.REPLAN_GLOBAL.value,
            reason="root assumption failed",
            scope="global",
            allowed=True,
            estimated_cost=0.1,
        )
        receipt = RecoveryGate().evaluate(
            record=record,
            task=item,
            failure=failure,
            decision=decision,
        )
        self.assertFalse(receipt.allowed)
        self.assertEqual(RecoveryAction.STOP.value, receipt.effective_action)

    def test_irreversible_replacement_requires_fresh_approval(self):
        item = task("migration")
        item.requires_tools = ["database_migration"]
        record = RunRecord(
            id="run_" + "5" * 32,
            request=RunRequest("goal"),
            plan=plan([item]),
        )
        failure = FailureRecord(
            failure_id="failure_" + "2" * 32,
            task_id=item.id,
            attempt=1,
            classification="contract_error",
            confidence=0.9,
            signals=("contract-shape-failure",),
            source="validation",
            created_at="2026-01-01T00:00:00Z",
        )
        decision = RecoveryDecision(
            action=RecoveryAction.REPLAN_SUBGRAPH.value,
            reason="repair",
            scope="subgraph",
            allowed=True,
            estimated_cost=0.1,
        )
        receipt = RecoveryGate().evaluate(
            record=record,
            task=item,
            failure=failure,
            decision=decision,
        )
        self.assertTrue(receipt.allowed)
        self.assertTrue(receipt.requires_approval)

    def test_replan_preserves_permissions_tools_and_dependency_contract(self):
        failed = task("failed")
        failed.data_permissions = ["public"]
        failed.requires_tools = ["web_search"]
        failed.dependency_types = {}
        failed.input_artifact_schemas = {"upstream": "artifact://x/1"}
        current = plan([failed])
        replacement = HeuristicPlanner().replan(current, failed.id)
        self.assertEqual(["public"], replacement.data_permissions)
        self.assertEqual(["web_search"], replacement.requires_tools)
        self.assertEqual(
            {"upstream": "artifact://x/1"},
            replacement.input_artifact_schemas,
        )


class HandoffRecoveryIntegrationTests(unittest.TestCase):
    def test_runtime_persists_handoffs_checkpoints_and_recovery_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = f"{directory}/runtime.sqlite3"
            manager = RunManager(
                runtime_db_path=path, recover=False
            )
            record = manager.create(
                RunRequest(
                    "研究并开发一个数据产品，最后撰写报告",
                    budget=4,
                    max_concurrency=4,
                    simulate_replan=True,
                ),
                idempotency_key="v055-integration-create-0001",
            )
            deadline = time.time() + 20
            while (
                time.time() < deadline
                and record.status
                not in {
                    RunStatus.COMPLETED,
                    RunStatus.FAILED,
                    RunStatus.CANCELLED,
                }
            ):
                time.sleep(0.03)
            self.assertEqual(
                RunStatus.COMPLETED, record.status, record.error
            )
            manager.threads[record.id].join(timeout=5)
            self.assertTrue(record.handoff_history)
            self.assertTrue(record.completion_checkpoints)
            self.assertEqual(1, record.global_replans)
            self.assertEqual(1, len(record.pollution_reports))
            self.assertTrue(
                all(
                    item["admitted"]
                    for item in record.handoff_history
                )
            )
            handoffs = manager.handoffs(record.id)
            self.assertEqual(
                len(record.handoff_history), len(handoffs)
            )
            self.assertTrue(
                all(item["contract_digest"] for item in handoffs)
            )
            failures = manager.failures(record.id)
            self.assertTrue(
                failures[0]["recovery_receipt"]["gate"][
                    "allowed"
                ]
            )
            restored_manager = RunManager(
                runtime_db_path=path, recover=False
            )
            restored = restored_manager.get(record.id)
            self.assertEqual(
                len(record.completion_checkpoints),
                len(restored.completion_checkpoints),
            )
            self.assertEqual(
                record.pollution_reports,
                restored.pollution_reports,
            )
            manager.shutdown()
            restored_manager.shutdown()


if __name__ == "__main__":
    unittest.main()
