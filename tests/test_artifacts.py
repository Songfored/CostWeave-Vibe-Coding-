import copy
import sqlite3
import tempfile
import unittest

from costweave.artifacts import (
    Artifact,
    ArtifactError,
    ArtifactIntegrityError,
    ArtifactStatus,
    StaleArtifactError,
)
from costweave.control_kernel import ControlKernel
from costweave.domain import RunRequest, TaskContract
from costweave.graph_compiler import GraphCompileError, GraphCompiler
from costweave.messages import (
    MessageEnvelope,
    MessageError,
    StalePlanRevisionError,
)
from costweave.planner import HeuristicPlanner
from costweave.projections import canonical_task_id


class ArtifactRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.kernel = ControlKernel(f"{self.temp.name}/runtime.sqlite3")
        self.record, _ = self.kernel.create_run(
            RunRequest("分析一个产品方案并形成报告").normalized(),
            idempotency_key="artifact-test-create-0001",
        )
        self.record.plan = HeuristicPlanner().plan(self.record.request)
        self.kernel.compile_graph(self.record)
        self.kernel.checkpoint(self.record, source="tests.artifact.plan")
        self.task = self.record.plan.tasks[0]
        self.task.attempt = 1

    def tearDown(self):
        self.temp.cleanup()

    def artifact(self, content=None, *, task=None, inputs=(), status=ArtifactStatus.VALIDATED):
        task = task or self.task
        spec = task.output_artifact_specs[0]
        return Artifact.create(
            run_id=self.record.id,
            producer_task_id=canonical_task_id(self.record.id, task.id),
            artifact_type=spec["artifact_type"],
            schema_ref=spec["schema_ref"],
            content=content or {"summary": "ok", "evidence": ["test"]},
            plan_revision=self.record.plan.version,
            status=status,
            producer_model="test-worker",
            input_artifact_refs=list(inputs),
            attempt=max(1, task.attempt),
        )

    def test_content_addressing_is_deterministic_and_versions_changes(self):
        first = self.artifact({"value": 1})
        same = self.artifact({"value": 1})
        changed = self.artifact({"value": 2})
        self.assertEqual(first.artifact_id, same.artifact_id)
        self.assertNotEqual(first.artifact_id, changed.artifact_id)

    def test_constructor_rejects_corrupted_content_hash(self):
        artifact = self.artifact()
        payload = artifact.to_dict()
        payload["content"]["summary"] = "tampered"
        with self.assertRaises(ArtifactIntegrityError):
            Artifact.from_dict(payload)

    def test_store_round_trip_preserves_lineage(self):
        parent, replayed = self.kernel.artifact_store.put(self.artifact())
        self.assertFalse(replayed)
        child_task = self.record.plan.tasks[1]
        child_task.attempt = 1
        child = self.artifact(
            {"summary": "child"},
            task=child_task,
            inputs=[parent.artifact_id],
        )
        self.kernel.artifact_store.put(child)
        restored = self.kernel.artifact_store.get(child.artifact_id)
        self.assertEqual((parent.artifact_id,), restored.input_artifact_refs)

    def test_missing_input_producer_is_rejected(self):
        child_task = self.record.plan.tasks[1]
        child_task.attempt = 1
        child = self.artifact(
            task=child_task,
            inputs=["artifact_" + "0" * 32],
        )
        with self.assertRaises(ArtifactError):
            self.kernel.artifact_store.put(child)

    def test_stale_input_is_rejected(self):
        parent, _ = self.kernel.artifact_store.put(self.artifact())
        self.kernel.artifact_store.mark_stale_for_tasks(
            self.record.id, [self.task.id]
        )
        child_task = self.record.plan.tasks[1]
        child_task.attempt = 1
        child = self.artifact(
            task=child_task,
            inputs=[parent.artifact_id],
        )
        with self.assertRaises(StaleArtifactError):
            self.kernel.artifact_store.put(child)

    def test_duplicate_artifact_write_is_idempotent(self):
        artifact = self.artifact()
        _, first_replay = self.kernel.artifact_store.put(artifact)
        _, second_replay = self.kernel.artifact_store.put(artifact)
        self.assertFalse(first_replay)
        self.assertTrue(second_replay)
        self.assertEqual(1, self.kernel.artifact_store.counts()["artifacts"])

    def test_database_tampering_is_detected_on_read(self):
        artifact, _ = self.kernel.artifact_store.put(self.artifact())
        connection = sqlite3.connect(self.kernel.store.path)
        try:
            connection.execute(
                "UPDATE artifacts SET content_json=? WHERE artifact_id=?",
                ('{"tampered":true}', artifact.artifact_id),
            )
            connection.commit()
        finally:
            connection.close()
        with self.assertRaises(ArtifactIntegrityError):
            self.kernel.artifact_store.get(artifact.artifact_id)

    def message(self, *, revision=None, objective=None):
        return MessageEnvelope.create(
            run_id=self.record.id,
            recipient_task_id=canonical_task_id(
                self.record.id, self.task.id
            ),
            plan_revision=revision or self.record.plan.version,
            attempt=1,
            goal=self.record.request.goal,
            objective=objective or self.task.objective,
            input_artifact_refs=[],
            output_artifact_specs=self.task.output_artifact_specs,
            acceptance_criteria=self.task.acceptance_criteria,
            include=self.task.include,
            exclude=self.task.exclude,
        )

    def test_duplicate_handoff_message_is_idempotent(self):
        message = self.message()
        _, first = self.kernel.artifact_store.put_message(message)
        _, second = self.kernel.artifact_store.put_message(message)
        self.assertFalse(first)
        self.assertTrue(second)
        self.assertEqual(1, self.kernel.artifact_store.counts()["messages"])

    def test_message_identity_conflict_is_rejected(self):
        message = self.message()
        self.kernel.artifact_store.put_message(message)
        changed = self.message(objective="different objective")
        self.assertEqual(message.message_id, changed.message_id)
        with self.assertRaises(MessageError):
            self.kernel.artifact_store.put_message(changed)

    def test_stale_plan_revision_is_rejected(self):
        with self.assertRaises(StalePlanRevisionError):
            self.kernel.artifact_store.put_message(
                self.message(revision=self.record.plan.version + 1)
            )

    def test_mark_stale_only_changes_selected_producer(self):
        first, _ = self.kernel.artifact_store.put(self.artifact())
        sibling_task = next(
            task
            for task in self.record.plan.tasks
            if task.id.startswith("branch-")
        )
        sibling_task.attempt = 1
        sibling, _ = self.kernel.artifact_store.put(
            self.artifact({"sibling": True}, task=sibling_task)
        )
        changed = self.kernel.artifact_store.mark_stale_for_tasks(
            self.record.id, [self.task.id]
        )
        self.assertEqual([first.artifact_id], changed)
        self.assertEqual(
            ArtifactStatus.STALE,
            self.kernel.artifact_store.get(first.artifact_id).status,
        )
        self.assertEqual(
            ArtifactStatus.VALIDATED,
            self.kernel.artifact_store.get(sibling.artifact_id).status,
        )


class GraphCompilerTests(unittest.TestCase):
    def setUp(self):
        self.request = RunRequest("分析一个产品方案并形成报告").normalized()
        self.plan = HeuristicPlanner().plan(self.request)
        self.compiler = GraphCompiler()
        self.compiler.compile("run_" + "1" * 32, self.plan)

    def test_cycle_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan.tasks[0].dependencies = [plan.tasks[-1].id]
        with self.assertRaises(GraphCompileError):
            self.compiler.compile("run_" + "1" * 32, plan)

    def test_missing_producer_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        plan.tasks[1].dependencies.append("missing-task")
        with self.assertRaises(GraphCompileError):
            self.compiler.compile("run_" + "1" * 32, plan)

    def test_schema_conflict_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        consumer = plan.tasks[1]
        producer = plan.tasks[0]
        consumer.input_artifact_schemas[producer.id] = "artifact://wrong/9"
        with self.assertRaises(GraphCompileError):
            self.compiler.compile("run_" + "1" * 32, plan)

    def test_permission_conflict_is_rejected(self):
        plan = copy.deepcopy(self.plan)
        producer = plan.tasks[0]
        consumer = plan.tasks[1]
        producer.output_artifact_specs[0]["sensitivity"] = "sensitive"
        consumer.data_permissions = ["public", "internal"]
        with self.assertRaises(GraphCompileError):
            self.compiler.compile("run_" + "1" * 32, plan)

    def test_affected_subgraph_preserves_unrelated_sibling(self):
        branches = [
            task for task in self.plan.tasks if task.id.startswith("branch-")
        ]
        if len(branches) < 2:
            self.plan.tasks.insert(
                -2,
                TaskContract(
                    id="branch-sibling",
                    title="Sibling",
                    objective="Independent work",
                    task_type="analysis",
                    dependencies=["goal-alignment"],
                    acceptance_criteria=["done"],
                    output_schema=["summary", "evidence", "confidence"],
                ),
            )
            self.compiler.compile("run_" + "1" * 32, self.plan)
            branches = [
                task
                for task in self.plan.tasks
                if task.id.startswith("branch-")
            ]
        affected = self.compiler.affected_subgraph(
            self.plan, {branches[0].id}
        )
        self.assertIn(branches[0].id, affected)
        self.assertNotIn(branches[1].id, affected)

    def test_dependency_kinds_are_materialized(self):
        graph = self.compiler.compile("run_" + "1" * 32, self.plan)
        self.assertTrue(graph.edges)
        self.assertTrue(
            {"hard", "validation"} & {edge["kind"] for edge in graph.edges}
        )


if __name__ == "__main__":
    unittest.main()
