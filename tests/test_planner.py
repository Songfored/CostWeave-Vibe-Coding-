import unittest

from costweave.domain import RunMode, RunRequest, TaskContract
from costweave.planner import HeuristicPlanner
from costweave.router import PredictiveRouter
from costweave.validator import ContractValidator


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.planner = HeuristicPlanner()
        self.validator = ContractValidator()

    def test_cross_domain_request_builds_parallel_dag(self):
        request = RunRequest("开发一个市场研究和数据分析系统，并形成最终报告")
        plan = self.planner.plan(request)
        ids = {task.id for task in plan.tasks}
        self.assertIn("branch-coding", ids)
        self.assertIn("branch-research", ids)
        self.assertIn("branch-data", ids)
        self.assertIn("branch-writing", ids)
        branches = [task for task in plan.tasks if task.parallel_group == "specialists"]
        self.assertGreaterEqual(len(branches), 3)
        self.assertTrue(all(task.dependencies == ["goal-alignment"] for task in branches))
        self.assertEqual([], self.validator.validate_plan(plan))

    def test_difficulty_is_bounded(self):
        simple = self.planner.plan(RunRequest("总结这份报告"))
        complex_plan = self.planner.plan(RunRequest("任意、所有、全能、完善；研究、开发、数据、报告；并提供最好方案" * 10))
        self.assertGreaterEqual(simple.difficulty, 1)
        self.assertLessEqual(complex_plan.difficulty, 5)
        self.assertGreater(complex_plan.difficulty, simple.difficulty)

    def test_contracts_have_acceptance_and_schema(self):
        plan = self.planner.plan(RunRequest("设计一个应用产品并撰写方案"))
        for task in plan.tasks:
            self.assertTrue(task.objective)
            self.assertTrue(task.acceptance_criteria)
            self.assertTrue(task.output_schema)

    def test_router_assigns_every_task_before_execution(self):
        plan = self.planner.plan(RunRequest("开发数据应用"))
        PredictiveRouter().assign(plan.tasks, RunMode.BALANCED, .78)
        for task in plan.tasks:
            self.assertIsNotNone(task.selected_worker)
            self.assertGreater(task.predicted_success, 0)
            self.assertGreater(task.estimated_latency_ms, 0)

    def test_replan_rewires_descendants(self):
        plan = self.planner.plan(RunRequest("开发一个应用"))
        failed_id = "branch-coding"
        replacement = self.planner.replan(plan, failed_id)
        self.assertEqual(2, plan.version)
        self.assertEqual("senior-planner", replacement.selected_worker)
        review = next(task for task in plan.tasks if task.id == "risk-review")
        self.assertIn(replacement.id, review.dependencies)
        self.assertNotIn(failed_id, review.dependencies)
        self.assertEqual([], self.validator.validate_plan(plan))


class ValidatorTests(unittest.TestCase):
    def test_cycle_is_rejected(self):
        plan = HeuristicPlanner().plan(RunRequest("分析一个方案"))
        first = plan.tasks[0]
        last = plan.tasks[-1]
        first.dependencies = [last.id]
        issues = ContractValidator().validate_plan(plan)
        self.assertTrue(any("循环" in issue for issue in issues))

    def test_invalid_result_is_rejected(self):
        task = TaskContract(
            id="x", title="x", objective="x", task_type="analysis",
            acceptance_criteria=["完整"], output_schema=["summary"],
        )
        outcome = ContractValidator().validate_result(task, {"summary": "only"})
        self.assertFalse(outcome["passed"])

    def test_empty_and_contract_incomplete_results_are_rejected(self):
        task = TaskContract(
            id="x", title="x", objective="x", task_type="risk",
            acceptance_criteria=["完整"],
            output_schema=["summary", "findings", "evidence", "confidence"],
        )
        empty = ContractValidator().validate_result(
            task,
            {"summary": "", "evidence": [""], "confidence": 0},
        )
        missing_contract_field = ContractValidator().validate_result(
            task,
            {"summary": "有内容", "evidence": ["证据"], "confidence": .8},
        )
        malformed_confidence = ContractValidator().validate_result(
            task,
            {"summary": "有内容", "findings": ["风险"], "evidence": ["证据"], "confidence": "not-a-number"},
        )
        self.assertFalse(empty["passed"])
        self.assertFalse(missing_contract_field["passed"])
        self.assertFalse(malformed_confidence["passed"])


if __name__ == "__main__":
    unittest.main()
