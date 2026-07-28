from __future__ import annotations

from collections import defaultdict, deque

from .domain import Plan, TaskContract, ValidationLevel
from .success_contract import SuccessContract, is_self_attestation


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
            unknown_critical = (
                set(task.critical_criteria)
                - set(task.acceptance_criteria)
            )
            if unknown_critical:
                issues.append(
                    f"{task.id} 的关键标准不在验收标准中："
                    f"{sorted(unknown_critical)}"
                )
            contract = SuccessContract.from_task(task)
            task.success_contract = contract.to_dict()
            task.critical_criteria = [
                item.text
                for item in contract.criteria
                if item.critical
            ]
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
        contract = (
            SuccessContract.from_dict(task.success_contract)
            if task.success_contract
            else SuccessContract.from_task(task)
        )
        task.success_contract = contract.to_dict()
        if not isinstance(result, dict):
            return {
                "passed": False,
                "level": ValidationLevel.SCHEMA.value,
                "findings": ["结果不是JSON对象"],
                "score": 0.0,
                "contract_revision": contract.revision,
                "criterion_results": [],
                "critical_failures": [
                    item.text for item in contract.criteria
                    if item.critical
                ],
                "evidence_gaps": ["结果不是JSON对象"],
                "contract_errors": ["结果不是JSON对象"],
                "layers_completed": [],
                "false_positive_risk": 0.01,
                "false_negative_risk": 0.2,
            }
        required = set(contract.required_result_fields)
        missing = sorted(required - set(result))
        malformed_confidence = False
        try:
            confidence = float(result.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
            malformed_confidence = True
        findings: list[str] = []
        base_passed = True
        contract_errors: list[str] = []
        evidence_gaps: list[str] = []

        if missing:
            findings.append(f"缺少必要字段：{', '.join(missing)}")
            contract_errors.append(
                f"缺少必要字段：{', '.join(missing)}"
            )
            base_passed = False
        if not 0 <= confidence <= 1:
            findings.append("置信度不在0到1范围")
            contract_errors.append("置信度不在0到1范围")
            base_passed = False
        if malformed_confidence:
            findings.append("置信度不是有效数字")
            contract_errors.append("置信度不是有效数字")
            base_passed = False
        if not str(result.get("summary", "")).strip():
            findings.append("摘要为空")
            contract_errors.append("摘要为空")
            base_passed = False
        evidence = result.get("evidence")
        if not isinstance(evidence, list) or not any(str(item).strip() for item in evidence):
            findings.append("缺少可追溯证据")
            evidence_gaps.append("缺少可追溯证据")
            base_passed = False
        gate_types = {"planning", "validation", "risk", "safety"}
        supplied_criteria = result.get("criteria_results")
        if task.task_type in gate_types:
            decision = result.get("decision")
            if decision not in {"pass", "revise", "reject", "human_review"}:
                findings.append("门控节点缺少有效 decision")
                contract_errors.append("门控节点缺少有效 decision")
                base_passed = False
            elif decision != "pass":
                findings.append(f"门控决定为 {decision}，不能解锁下游")
                base_passed = False
            if not isinstance(supplied_criteria, list):
                findings.append("门控节点缺少逐条验收结果")
                contract_errors.append("门控节点缺少逐条验收结果")
                base_passed = False
        if result.get("fatal_error"):
            findings.append(str(result["fatal_error"]))
            base_passed = False

        supplied_by_text = {
            str(item.get("criterion", "")).strip(): item
            for item in (
                supplied_criteria
                if isinstance(supplied_criteria, list)
                else []
            )
            if isinstance(item, dict)
        }
        criterion_results = []
        for criterion in contract.criteria:
            supplied = supplied_by_text.get(criterion.text)
            if supplied is None and task.task_type in gate_types:
                claimed_pass = False
                evidence_refs: list[object] = []
                reason = "门控结果没有覆盖该标准"
            elif supplied is None:
                evidence_refs = (
                    list(evidence)
                    if isinstance(evidence, list)
                    else []
                )
                claimed_pass = bool(
                    base_passed and evidence_refs
                )
                reason = (
                    "由 Schema、合同字段和外部证据确定性检查"
                    if claimed_pass
                    else "缺少确定性合同证据"
                )
            else:
                claimed_pass = supplied.get("passed") is True
                refs = supplied.get("evidence_refs", [])
                evidence_refs = (
                    list(refs) if isinstance(refs, list) else []
                )
                reason = str(
                    supplied.get(
                        "reason",
                        "执行结果提供逐条标准声明",
                    )
                )
            independent = bool(evidence_refs) and not all(
                is_self_attestation(item) for item in evidence_refs
            )
            criterion_passed = (
                claimed_pass
                and bool(evidence_refs)
                and (
                    not criterion.independent_evidence_required
                    or independent
                )
            )
            if criterion.critical and not independent:
                criterion_passed = False
                reason = "关键标准不能只由执行模型自评解锁"
            if not criterion_passed:
                evidence_gaps.append(
                    f"{criterion.text}: {reason}"
                )
            criterion_results.append({
                "criterion_id": criterion.criterion_id,
                "criterion": criterion.text,
                "critical": criterion.critical,
                "passed": criterion_passed,
                "evidence_refs": [
                    str(item) for item in evidence_refs
                ],
                "independent_evidence": independent,
                "reason": reason,
            })

        critical_failures = [
            item["criterion"]
            for item in criterion_results
            if item["critical"] and not item["passed"]
        ]
        criterion_score = (
            sum(item["passed"] for item in criterion_results)
            / len(criterion_results)
            if criterion_results
            else 0.0
        )
        score = min(confidence, criterion_score)
        passed = (
            base_passed
            and not critical_failures
            and score + 1e-12 >= contract.minimum_score
        )
        if critical_failures:
            findings.append(
                "关键标准失败：" + "；".join(critical_failures)
            )
        noncritical_failures = [
            item["criterion"]
            for item in criterion_results
            if not item["critical"] and not item["passed"]
        ]
        if noncritical_failures:
            findings.append(
                "普通质量标准未通过："
                + "；".join(noncritical_failures)
            )
        if (
            not critical_failures
            and score < contract.minimum_score
        ):
            findings.append(
                f"验收分数 {score:.3f} 低于合同门槛 "
                f"{contract.minimum_score:.3f}"
            )
        if passed:
            findings.append("Schema、合同字段、范围和最低证据要求通过")

        level = ValidationLevel.SCHEMA
        if task.task_type in {"validation", "risk", "safety"}:
            level = ValidationLevel.SPECIALIST
        if task.risk_level == "high" or task.difficulty >= 9:
            level = ValidationLevel.SENIOR
        independent_ratio = (
            sum(
                item["independent_evidence"]
                for item in criterion_results
            )
            / len(criterion_results)
            if criterion_results
            else 0.0
        )
        layers = ["L0-schema"]
        if not contract_errors:
            layers.append("L1-deterministic")
        if independent_ratio > 0:
            layers.append("L2-independent-evidence")
        if level == ValidationLevel.SENIOR:
            layers.append("L3-senior-policy")
        false_positive_risk = min(
            0.95,
            max(
                0.01,
                0.34
                - independent_ratio * 0.2
                + len(critical_failures) * 0.18
                + (0.12 if contract_errors else 0),
            ),
        )
        false_negative_risk = min(
            0.8,
            max(
                0.02,
                0.06
                + task.uncertainty * 0.16
                + (0.08 if not passed else 0),
            ),
        )
        return {
            "passed": passed,
            "level": level.value,
            "findings": findings,
            "score": round(
                score if passed else min(score, .35), 3
            ),
            "contract_revision": contract.revision,
            "minimum_score": contract.minimum_score,
            "criterion_results": criterion_results,
            "critical_failures": critical_failures,
            "evidence_gaps": evidence_gaps,
            "contract_errors": contract_errors,
            "layers_completed": layers,
            "false_positive_risk": round(
                false_positive_risk, 4
            ),
            "false_negative_risk": round(
                false_negative_risk, 4
            ),
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
