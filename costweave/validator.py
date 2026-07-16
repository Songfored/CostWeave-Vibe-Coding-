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
        if self._has_cycle(plan.tasks):
            issues.append("任务图存在循环依赖")
        if "final-synthesis" not in known:
            issues.append("缺少最终汇总节点")
        return issues

    def validate_result(self, task: TaskContract, result: dict) -> dict:
        missing = sorted(self.REQUIRED_RESULT_FIELDS - set(result))
        confidence = float(result.get("confidence", 0.0))
        findings: list[str] = []
        passed = True

        if missing:
            findings.append(f"缺少必要字段：{', '.join(missing)}")
            passed = False
        if not 0 <= confidence <= 1:
            findings.append("置信度不在0到1范围")
            passed = False
        if not result.get("evidence"):
            findings.append("缺少可追溯证据")
            passed = False
        if result.get("fatal_error"):
            findings.append(str(result["fatal_error"]))
            passed = False
        if passed:
            findings.append("Schema、范围和最低证据要求通过")

        level = ValidationLevel.SCHEMA
        if task.task_type in {"risk", "synthesis"}:
            level = ValidationLevel.SPECIALIST
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

