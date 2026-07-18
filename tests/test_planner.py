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
        self.assertLessEqual(complex_plan.difficulty, 10)
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
        self.assertEqual(4, plan.version)
        self.assertIsNone(replacement.selected_worker)
        verifier = next(task for task in plan.tasks if task.id == "verify-coding")
        self.assertIn(replacement.id, verifier.dependencies)
        self.assertNotIn(failed_id, verifier.dependencies)
        self.assertEqual([], self.validator.validate_plan(plan))

    def test_fresh_high_stakes_goal_gets_tools_and_safety_gate(self):
        plan = self.planner.plan(RunRequest("搜索最新金融法规，分析数据并形成投资风险报告"))
        self.assertTrue(plan.analysis["needs_fresh_data"])
        self.assertEqual("high", plan.analysis["stakes"])
        self.assertIn("domain-safety-review", {task.id for task in plan.tasks})
        research = next(task for task in plan.tasks if task.id == "branch-research")
        self.assertIn("web_search", research.requires_tools)
        self.assertTrue(research.handoff_prompt)

    def test_each_specialist_branch_has_independent_verifier(self):
        plan = self.planner.plan(RunRequest("开发一个市场数据分析系统并撰写报告"))
        branches = [task for task in plan.tasks if task.parallel_group == "specialists"]
        verifier_dependencies = {
            dependency
            for task in plan.tasks if task.parallel_group == "verification"
            for dependency in task.dependencies
        }
        self.assertEqual({task.id for task in branches}, verifier_dependencies)

    def test_hierarchical_profile_avoids_shallow_keyword_false_positives(self):
        chinese_copy = self.planner.plan(RunRequest("请用中文写一份产品介绍"))
        api_price = self.planner.plan(RunRequest("分析 API 调用价格并形成成本报告"))
        self.assertNotIn("translation", chinese_copy.task_types)
        self.assertNotIn("coding", api_price.task_types)
        self.assertIn("primary_intent", chinese_copy.analysis)
        self.assertIn("classifications", chinese_copy.analysis)
        self.assertGreater(chinese_copy.analysis["classification_confidence"], .5)

    def test_primary_intent_uses_evidence_position_instead_of_rule_order(self):
        plan = self.planner.plan(
            RunRequest("分析最新模型价格与能力，然后生成一份比较方案")
        )
        self.assertEqual("analyze", plan.analysis["primary_intent"])
        self.assertEqual("analyze", plan.analysis["intent_scores"][0]["label"])
        self.assertIn("build", plan.analysis["operations"])

    def test_complexity_profile_and_branch_difficulty_are_task_specific(self):
        plan = self.planner.plan(
            RunRequest("开发一个数据分析应用，并撰写面向用户的介绍")
        )
        coding = next(task for task in plan.tasks if task.id == "branch-coding")
        writing = next(task for task in plan.tasks if task.id == "branch-writing")
        self.assertGreater(coding.difficulty, writing.difficulty)
        self.assertEqual("3.0", plan.analysis["taxonomy_version"])
        self.assertIn("cognitive", plan.analysis["complexity"])


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

    def test_reject_decision_cannot_unlock_downstream(self):
        task = TaskContract(
            id="verify", title="验收", objective="验收", task_type="validation",
            acceptance_criteria=["逐条检查"],
            output_schema=[
                "summary", "criteria_results", "findings",
                "evidence", "decision", "confidence",
            ],
        )
        outcome = ContractValidator().validate_result(task, {
            "summary": "格式完整但结论拒绝",
            "criteria_results": [{
                "criterion": "逐条检查",
                "passed": True,
                "evidence_refs": ["e1"],
            }],
            "findings": ["发现关键错误"],
            "evidence": ["e1"],
            "decision": "reject",
            "confidence": .92,
        })
        self.assertFalse(outcome["passed"])
        self.assertTrue(any("不能解锁" in item for item in outcome["findings"]))


if __name__ == "__main__":
    unittest.main()
