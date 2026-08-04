import os
import tempfile
import time
import unittest
from unittest.mock import patch

from costweave.control_kernel import ControlKernel
from costweave.domain import RunRequest, RunStatus, TaskStatus
from costweave.engine import RunManager
from costweave.event_store import RevisionConflictError


class ControlKernelTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = f"{self.temp.name}/runtime.sqlite3"

    def tearDown(self):
        self.temp.cleanup()

    @staticmethod
    def wait(record, timeout=12):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if record.status in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}:
                return
            time.sleep(0.03)
        raise AssertionError(f"run did not finish: {record.status}")

    def test_create_command_is_idempotent_and_keeps_one_run(self):
        kernel = ControlKernel(self.path)
        request = RunRequest("分析一个产品需求并形成报告").normalized()
        first, replayed_first = kernel.create_run(
            request, idempotency_key="kernel-create-key-0001"
        )
        second, replayed_second = kernel.create_run(
            request, idempotency_key="kernel-create-key-0001"
        )
        self.assertFalse(replayed_first)
        self.assertTrue(replayed_second)
        self.assertEqual(first.id, second.id)
        self.assertEqual(1, kernel.store.counts()["runs"])
        self.assertEqual(1, kernel.store.counts()["events"])

    def test_cancel_is_persistent_idempotent_and_terminal(self):
        kernel = ControlKernel(self.path)
        record, _ = kernel.create_run(
            RunRequest("分析一个需要取消的任务").normalized(),
            idempotency_key="cancel-create-key-0001",
        )
        kernel.cancel_run(
            record,
            idempotency_key="cancel-command-key-0001",
            expected_revision=record.runtime_revision,
        )
        revision = record.runtime_revision
        self.assertEqual(RunStatus.CANCELLED, record.status)
        kernel.cancel_run(
            record,
            idempotency_key="cancel-command-key-0001",
            expected_revision=1,
        )
        self.assertEqual(revision, record.runtime_revision)
        restored = ControlKernel(self.path).load_records()[0]
        self.assertEqual(RunStatus.CANCELLED, restored.status)

    def test_stale_cancel_revision_writes_nothing(self):
        kernel = ControlKernel(self.path)
        record, _ = kernel.create_run(
            RunRequest("分析一个并发取消任务").normalized(),
            idempotency_key="stale-cancel-create-0001",
        )
        before = kernel.store.counts()
        with self.assertRaises(RevisionConflictError):
            kernel.cancel_run(
                record,
                idempotency_key="stale-cancel-command-0001",
                expected_revision=0,
            )
        self.assertEqual(before, kernel.store.counts())
        self.assertEqual(RunStatus.CREATED, record.status)

    def test_completed_run_restores_plan_metrics_and_timeline(self):
        with patch.dict(os.environ, {"COSTWEAVE_RUNTIME_V4": "1"}):
            manager = RunManager(runtime_db_path=self.path, recover=False)
            record = manager.create(
                RunRequest(
                    "开发一个市场数据分析应用并形成报告",
                    budget=3,
                    max_concurrency=4,
                ),
                idempotency_key="complete-run-key-0001",
            )
            self.wait(record)
            manager.threads[record.id].join(timeout=3)
            self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
            revision = record.runtime_revision
            event_count = len(manager.events(record.id))
            restarted = RunManager(runtime_db_path=self.path, recover=False)
            restored = restarted.get(record.id)
        self.assertEqual(RunStatus.COMPLETED, restored.status)
        self.assertEqual(revision, restored.runtime_revision)
        self.assertEqual(event_count, len(restarted.events(restored.id)))
        self.assertIsNotNone(restored.plan)
        self.assertGreater(restored.spent, 0)

    def test_terminal_run_is_not_resumed_on_startup(self):
        with patch.dict(os.environ, {"COSTWEAVE_RUNTIME_V4": "1"}):
            first = RunManager(runtime_db_path=self.path, recover=False)
            record = first.create(
                RunRequest("分析一个产品并形成最终报告", budget=2),
                idempotency_key="terminal-run-key-0001",
            )
            self.wait(record)
            first.threads[record.id].join(timeout=3)
            restarted = RunManager(runtime_db_path=self.path, recover=True)
        self.assertNotIn(record.id, restarted.threads)
        self.assertEqual(RunStatus.COMPLETED, restarted.get(record.id).status)

    def test_unfinished_run_resumes_once_and_preserves_validated_work(self):
        kernel = ControlKernel(self.path)
        request = RunRequest(
            "开发一个数据分析应用并形成报告",
            budget=3,
            max_concurrency=3,
        ).normalized()
        record, _ = kernel.create_run(
            request, idempotency_key="unfinished-create-key-0001"
        )
        manager_seed = RunManager(runtime_db_path=self.path, recover=False)
        router = manager_seed.engine.router.freeze()
        record.worker_snapshot = [worker.to_dict() for worker in router.workers]
        record.plan = manager_seed.engine.planner.plan(request)
        record.plan.model_snapshot = router.catalog_revision
        record.plan.routing_summary = router.assign(
            record.plan.tasks,
            request.mode,
            request.quality_floor,
            request.budget,
        )
        kernel.set_run_status(record, RunStatus.PLANNING, source="tests.seed.planning")
        kernel.set_run_status(record, RunStatus.EXECUTING, source="tests.seed.executing")

        completed = record.plan.tasks[0]
        kernel.set_task_status(record, completed, TaskStatus.RUNNING, source="tests.seed.started")
        completed.attempt = 1
        completed.result = {"summary": "已锁定目标", "evidence": ["seed"], "confidence": 0.9}
        completed.validation = {"passed": True, "level": "L1", "findings": [], "score": 0.9}
        kernel.set_task_status(record, completed, TaskStatus.VALIDATED, source="tests.seed.validated")

        interrupted = next(
            task for task in record.plan.tasks
            if completed.id in task.dependencies
        )
        kernel.set_task_status(record, interrupted, TaskStatus.RUNNING, source="tests.seed.interrupted")
        interrupted.attempt = 1
        kernel.checkpoint(record, source="tests.seed.checkpoint")

        with patch.dict(os.environ, {"COSTWEAVE_RUNTIME_V4": "1"}):
            restarted = RunManager(runtime_db_path=self.path, recover=True)
            restored = restarted.get(record.id)
            self.wait(restored, timeout=12)
            restarted.threads[record.id].join(timeout=3)
        self.assertEqual(RunStatus.COMPLETED, restored.status, restored.error)
        restored_completed = next(task for task in restored.plan.tasks if task.id == completed.id)
        restored_interrupted = next(task for task in restored.plan.tasks if task.id == interrupted.id)
        self.assertEqual(1, restored_completed.attempt)
        self.assertEqual(2, restored_interrupted.attempt)
        self.assertEqual(1, len([name for name in restarted.threads if name == record.id]))

    def test_runtime_flag_disabled_does_not_create_database(self):
        disabled_path = f"{self.temp.name}/disabled.sqlite3"
        with patch.dict(os.environ, {"COSTWEAVE_RUNTIME_V4": "0"}):
            manager = RunManager(runtime_db_path=disabled_path, recover=False)
            self.assertIsNone(manager.control_kernel)
            self.assertFalse(os.path.exists(disabled_path))

    def test_artifact_flag_disables_new_artifact_writes(self):
        with patch.dict(
            os.environ,
            {
                "COSTWEAVE_CONTRACTS_V4": "1",
                "COSTWEAVE_RUNTIME_V4": "1",
                "COSTWEAVE_ARTIFACT_DAG": "0",
            },
        ):
            manager = RunManager(
                runtime_db_path=self.path,
                recover=False,
            )
            record = manager.create(
                RunRequest(
                    "分析一个关闭 Artifact DAG 的兼容任务",
                    budget=2,
                ),
                idempotency_key="artifact-disabled-create-0001",
            )
            self.wait(record)
            self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
            self.assertEqual([], manager.artifacts(record.id))
            self.assertEqual(
                0,
                manager.control_kernel.artifact_store.counts()["artifacts"],
            )
            manager.shutdown()

    def test_replan_stales_only_affected_artifacts_and_finishes(self):
        manager = RunManager(runtime_db_path=self.path, recover=False)
        record = manager.create(
            RunRequest(
                "研究并开发一个数据产品，最终撰写报告",
                budget=3,
                max_concurrency=4,
                simulate_replan=True,
            ),
            idempotency_key="artifact-replan-create-0001",
        )
        self.wait(record, timeout=15)
        self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
        artifacts = manager.artifacts(record.id)
        stale = [item for item in artifacts if item["status"] == "stale"]
        validated = [
            item for item in artifacts if item["status"] == "validated"
        ]
        self.assertTrue(stale)
        self.assertTrue(validated)
        self.assertTrue(
            any("recovery-v" in task.id for task in record.plan.tasks)
        )
        self.assertEqual(
            len(record.plan.tasks),
            len(validated)
            + len(
                [
                    task
                    for task in record.plan.tasks
                    if task.status == TaskStatus.INVALIDATED
                ]
            ),
        )
        manager.shutdown()


if __name__ == "__main__":
    unittest.main()
