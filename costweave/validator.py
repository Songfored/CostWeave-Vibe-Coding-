from __future__ import annotations

from collections import defaultdict, deque

from .domain import Plan, TaskContract, ValidationLevel


class PlanValidationError(ValueError):
    pass


class ContractValidator:
    REQUIRED_RESULT_FIELDS = {"summary", "evidence", "confidence"}

    def validate_plan(self, plan: Plan) -> list[str]:
        issues: list[str] = []
        ids = [task.id for task in plan.tasks]
        if len(ids) != len(set(ids)):
            issues.append("任务ID不唯一")
        known = set(ids)
        for task in plan.tasks:
            unknown = set(task.dependencies) - known
            if unknown:
                issues.append(f"{task.id} 引用了未知依赖：{sorted(unknown)}")
            if not task.objective or not task.acceptance_criteria or not task.output_schema:
                issues.append(f"{task.id} 的任务合同不完整")
            if not 1 <= task.difficulty <= 10:
                issues.append(f"{task.id} 的难度不在1到10之间")
            if not 0 <= task.uncertainty <= 1:
                issues.append(f"{task.id} 的不确定性不在0到1之间")
            if not 0 <= task.criticality <= 1:
                issues.append(f"{task.id} 的关键度不在0到1之间")
        if self._has_cycle(plan.tasks):
            issues.append("任务图存在循环依赖")
        if "final-synthesis" not in known:
            issues.append("缺少最终汇总节点")
        return issues

    def validate_result(self, task: TaskContract, result: dict) -> dict:
        if not isinstance(result, dict):
            return {
                "passed": False,
                "level": ValidationLevel.SCHEMA.value,
                "findings": ["结果不是JSON对象"],
                "score": 0.0,
            }
        required = self.REQUIRED_RESULT_FIELDS | set(task.output_schema)
        missing = sorted(required - set(result))
        malformed_confidence = False
        try:
            confidence = float(result.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
            malformed_confidence = True
        findings: list[str] = []
        passed = True

        if missing:
            findings.append(f"缺少必要字段：{', '.join(missing)}")
            passed = False
        if not 0 <= confidence <= 1:
            findings.append("置信度不在0到1范围")
            passed = False
        if malformed_confidence:
            findings.append("置信度不是有效数字")
            passed = False
        if not str(result.get("summary", "")).strip():
            findings.append("摘要为空")
            passed = False
        evidence = result.get("evidence")
        if not isinstance(evidence, list) or not any(str(item).strip() for item in evidence):
            findings.append("缺少可追溯证据")
            passed = False
        gate_types = {"planning", "validation", "risk", "safety"}
        if task.task_type in gate_types:
            decision = result.get("decision")
            if decision not in {"pass", "revise", "reject", "human_review"}:
                findings.append("门控节点缺少有效 decision")
                passed = False
            elif decision != "pass":
                findings.append(f"门控决定为 {decision}，不能解锁下游")
                passed = False
            criteria_results = result.get("criteria_results")
            if not isinstance(criteria_results, list):
                findings.append("门控节点缺少逐条验收结果")
                passed = False
            else:
                covered = {
                    str(item.get("criterion", "")).strip()
                    for item in criteria_results
                    if isinstance(item, dict) and item.get("passed") is True
                }
                missing_criteria = [
                    criterion for criterion in task.acceptance_criteria
                    if criterion not in covered
                ]
                if missing_criteria:
                    findings.append(
                        "未通过的验收标准：" + "；".join(missing_criteria)
                    )
                    passed = False
        if result.get("fatal_error"):
            findings.append(str(result["fatal_error"]))
            passed = False
        if passed:
            findings.append("Schema、合同字段、范围和最低证据要求通过")

        level = ValidationLevel.SCHEMA
        if task.task_type in {"validation", "risk", "safety"}:
            level = ValidationLevel.SPECIALIST
        if task.risk_level == "high" or task.difficulty >= 9:
            level = ValidationLevel.SENIOR
        return {
            "passed": passed,
            "level": level.value,
            "findings": findings,
            "score": round(confidence if passed else min(confidence, .35), 3),
        }

    @staticmethod
    def descendants(tasks: list[TaskContract], root: str) -> set[str]:
        children: dict[str, list[str]] = defaultdict(list)
        for task in tasks:
            for dependency in task.dependencies:
                children[dependency].append(task.id)
        found: set[str] = set()
        queue = deque([root])
        while queue:
            current = queue.popleft()
            for child in children[current]:
                if child not in found:
                    found.add(child)
                    queue.append(child)
        return found

    @staticmethod
    def _has_cycle(tasks: list[TaskContract]) -> bool:
        indegree = {task.id: len(task.dependencies) for task in tasks}
        children: dict[str, list[str]] = defaultdict(list)
        for task in tasks:
            for dep in task.dependencies:
                children[dep].append(task.id)
        queue = deque(task_id for task_id, count in indegree.items() if count == 0)
        visited = 0
        while queue:
            current = queue.popleft()
            visited += 1
            for child in children[current]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    queue.append(child)
        return visited != len(tasks)
