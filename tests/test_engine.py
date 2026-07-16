import asyncio
import unittest

from costweave.domain import RunMode, RunRequest, RunStatus, TaskStatus
from costweave.engine import OrchestrationEngine, RunRecord
from costweave.executor import SimulatedExecutor


class OneShotFailingExecutor(SimulatedExecutor):
    def __init__(self):
        self.failed = False

    async def execute(self, task, worker, dependency_results, inject_fatal_error=False):
        if not self.failed and task.id == "goal-alignment":
            self.failed = True
            raise RuntimeError("temporary adapter failure")
        return await super().execute(task, worker, dependency_results, inject_fatal_error)


class EngineTests(unittest.IsolatedAsyncioTestCase):
    async def test_normal_run_completes_with_parallelism(self):
        record = RunRecord(
            id="normal",
            request=RunRequest(
                "开发一个市场数据分析应用并形成报告",
                mode=RunMode.BALANCED,
                budget=2.0,
                max_concurrency=4,
            ).normalized(),
        )
        await OrchestrationEngine().run(record)
        self.assertEqual(RunStatus.COMPLETED, record.status)
        self.assertGreaterEqual(record.peak_parallelism, 2)
        self.assertEqual(0, record.replans)
        self.assertTrue(all(task.status == TaskStatus.VALIDATED for task in record.plan.tasks))
        self.assertGreater(record.current_success_probability, .75)

    async def test_fatal_midpoint_error_triggers_replan(self):
        record = RunRecord(
            id="replan",
            request=RunRequest(
                "研究并开发一个数据产品，最终撰写报告",
                budget=3.0,
                max_concurrency=4,
                simulate_replan=True,
            ).normalized(),
        )
        await OrchestrationEngine().run(record)
        self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
        self.assertEqual(1, record.replans)
        self.assertEqual(2, record.plan.version)
        invalidated = [task for task in record.plan.tasks if task.status == TaskStatus.INVALIDATED]
        recovery = [task for task in record.plan.tasks if "recovery-v2" in task.id]
        self.assertEqual(1, len(invalidated))
        self.assertEqual(1, len(recovery))
        self.assertEqual(TaskStatus.VALIDATED, recovery[0].status)
        self.assertTrue(any(event.kind == "replanning" for event in record.events))
        self.assertGreater(record.current_success_probability, .80)

    async def test_budget_guard_stops_run(self):
        record = RunRecord(
            id="budget",
            request=RunRequest("开发数据报告应用", budget=.05, max_concurrency=3).normalized(),
        )
        await OrchestrationEngine().run(record)
        self.assertEqual(RunStatus.FAILED, record.status)
        self.assertIn("预算", record.error)
        self.assertLessEqual(record.spent, record.request.budget)
        self.assertFalse(any(task.status == TaskStatus.RUNNING for task in record.plan.tasks))

    async def test_quality_floor_is_a_hard_routing_constraint(self):
        record = RunRecord(
            id="quality-floor",
            request=RunRequest("分析并撰写产品方案", quality_floor=.99, budget=2).normalized(),
        )
        await OrchestrationEngine().run(record)
        self.assertEqual(RunStatus.FAILED, record.status)
        self.assertIn("质量门槛", record.error)

    async def test_adapter_exception_is_replanned_without_dirty_running_tasks(self):
        engine = OrchestrationEngine()
        engine.executor = OneShotFailingExecutor()
        record = RunRecord(
            id="adapter-recovery",
            request=RunRequest("分析一个产品方案", budget=3).normalized(),
        )
        await engine.run(record)
        self.assertEqual(RunStatus.COMPLETED, record.status, record.error)
        self.assertEqual(1, record.replans)
        self.assertFalse(any(task.status == TaskStatus.RUNNING for task in record.plan.tasks))


if __name__ == "__main__":
    unittest.main()
