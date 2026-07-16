from __future__ import annotations

import re

from .domain import Plan, RunRequest, TaskContract


TYPE_RULES = {
    "coding": ("代码", "开发", "程序", "软件", "接口", "api", "bug", "系统", "网站", "应用"),
    "research": ("研究", "调研", "资料", "市场", "竞品", "来源", "事实", "搜索"),
    "data": ("数据", "统计", "分析", "指标", "计算", "报表", "预测", "趋势"),
    "writing": ("文章", "报告", "方案", "文案", "总结", "撰写", "演讲", "邮件"),
}


class HeuristicPlanner:
    """A deterministic placeholder for the future local planning model."""

    def analyze(self, request: RunRequest) -> tuple[int, list[str], list[str], float]:
        goal = request.goal.lower()
        task_types = [kind for kind, words in TYPE_RULES.items() if any(word in goal for word in words)]
        if not task_types:
            task_types = ["analysis", "writing"]

        separators = len(re.findall(r"[，,；;、\n]", goal))
        ambiguity = sum(word in goal for word in ("任意", "所有", "全能", "最好", "完善", "等等"))
        cross_domain = max(0, len(task_types) - 1)
        raw = 1 + len(goal) // 80 + separators // 3 + ambiguity + cross_domain
        difficulty = max(1, min(5, raw))

        risks: list[str] = []
        if len(goal) < 14:
            risks.append("目标描述较短，验收边界可能不完整")
        if ambiguity:
            risks.append("存在宽泛词语，需要在任务合同中收紧范围")
        if cross_domain >= 2:
            risks.append("跨领域成果可能出现口径冲突")
        if request.budget < .25:
            risks.append("预算较低，复杂节点的候选执行者受限")
        if not risks:
            risks.append("模拟执行器无法验证真实领域事实")

        confidence = max(.56, min(.94, .92 - ambiguity * .08 - cross_domain * .025))
        return difficulty, task_types, risks, confidence

    def plan(self, request: RunRequest) -> Plan:
        difficulty, task_types, risks, estimate_confidence = self.analyze(request)
        tasks: list[TaskContract] = [self._alignment(request)]

        branch_ids: list[str] = []
        for task_type in task_types:
            task = self._branch(task_type, request)
            if task.id not in branch_ids:
                tasks.append(task)
                branch_ids.append(task.id)

        risk = TaskContract(
            id="risk-review",
            title="独立风险审查",
            objective="检查各并行成果的假设、遗漏、冲突和不可验证项。",
            task_type="risk",
            dependencies=branch_ids.copy(),
            required_capabilities=["risk", "validation"],
            acceptance_criteria=["列出关键风险", "指出冲突来源", "给出是否可进入汇总的判定"],
            output_schema=["summary", "findings", "evidence", "confidence"],
            include=["所有分支成果", "失败影响范围"],
            exclude=["代替最终汇总"],
            priority=9,
            parallel_group="review",
        )
        tasks.append(risk)

        tasks.append(TaskContract(
            id="final-synthesis",
            title="最终成果拼接",
            objective=f"围绕“{request.goal}”整合已经验收的成果，形成一致、可追溯的交付。",
            task_type="synthesis",
            dependencies=[*branch_ids, "risk-review"],
            required_capabilities=["synthesis", "writing", "structure"],
            acceptance_criteria=["覆盖原始目标", "不引入未验收内容", "显式保留风险与不确定性"],
            output_schema=["summary", "deliverable", "evidence", "confidence"],
            include=["通过验收的成果", "风险审查结论"],
            exclude=["被拒绝或失效的中间成果"],
            priority=10,
            parallel_group="synthesis",
        ))

        base_success = .94 - difficulty * .045 - max(0, len(task_types) - 2) * .025
        return Plan(
            version=1,
            difficulty=difficulty,
            task_types=task_types,
            rationale=f"识别到 {len(task_types)} 类能力需求，采用一项对齐任务、{len(branch_ids)} 个并行分支、独立审查和最终汇总。",
            tasks=tasks,
            predicted_success=max(.52, min(.94, base_success)),
            estimate_confidence=estimate_confidence,
            risks=risks,
        )

    def replan(self, plan: Plan, failed_task_id: str) -> TaskContract:
        plan.version += 1
        replacement_id = f"{failed_task_id}-recovery-v{plan.version}"
        failed = next(task for task in plan.tasks if task.id == failed_task_id)
        replacement = TaskContract(
            id=replacement_id,
            title=f"重构：{failed.title}",
            objective=f"高级规划顾问根据失败证据重新执行“{failed.objective}”，纠正原有假设并保留可验证成果。",
            task_type=failed.task_type,
            dependencies=failed.dependencies.copy(),
            required_capabilities=list(dict.fromkeys([*failed.required_capabilities, "planning", "risk"])),
            acceptance_criteria=[*failed.acceptance_criteria, "明确说明原方案为何失败", "输出与下游合同保持兼容"],
            output_schema=failed.output_schema.copy(),
            include=[*failed.include, "失败证据", "已验证的独立成果"],
            exclude=failed.exclude.copy(),
            priority=10,
            parallel_group="recovery",
            selected_worker="senior-planner",
            predicted_success=.96,
            estimated_cost=.095,
            estimated_latency_ms=754,
            plan_version=plan.version,
        )
        for task in plan.tasks:
            task.dependencies = [replacement_id if dep == failed_task_id else dep for dep in task.dependencies]
        plan.tasks.append(replacement)
        plan.predicted_success = min(.95, plan.predicted_success + .07)
        plan.risks.append(f"计划v{plan.version}已替换失败节点 {failed_task_id}")
        return replacement

    @staticmethod
    def _alignment(request: RunRequest) -> TaskContract:
        return TaskContract(
            id="goal-alignment",
            title="目标与约束对齐",
            objective=f"将用户目标“{request.goal}”转换成可验收的范围、约束与成功条件。",
            task_type="analysis",
            required_capabilities=["analysis", "structure"],
            acceptance_criteria=["保留用户核心目标", "列出范围边界", "形成可追踪的成功条件"],
            output_schema=["summary", "requirements", "constraints", "confidence"],
            include=["目标", "预算", "质量和速度偏好"],
            exclude=["开始具体领域执行"],
            priority=10,
            parallel_group="alignment",
        )

    @staticmethod
    def _branch(task_type: str, request: RunRequest) -> TaskContract:
        specs = {
            "coding": ("技术方案与实现", "设计可实现的模块、接口和验证策略。", ["coding", "planning", "testing"]),
            "research": ("证据与资料梳理", "建立所需事实、来源和未决问题清单。", ["research", "analysis"]),
            "data": ("数据与指标分析", "定义指标口径、计算路径和可复现验证方法。", ["data", "analysis", "validation"]),
            "writing": ("内容结构与表达", "形成服务于最终目标的内容结构和表达约束。", ["writing", "analysis", "structure"]),
            "analysis": ("核心问题分析", "识别关键决策、约束、假设与可行路径。", ["analysis", "planning"]),
        }
        title, objective, capabilities = specs[task_type]
        return TaskContract(
            id=f"branch-{task_type}",
            title=title,
            objective=f"针对“{request.goal}”：{objective}",
            task_type=task_type,
            dependencies=["goal-alignment"],
            required_capabilities=capabilities,
            acceptance_criteria=["输出符合合同Schema", "明确证据与假设", "不越出分配范围"],
            output_schema=["summary", "deliverable", "evidence", "assumptions", "confidence"],
            include=[f"{task_type}相关目标", "上游已验证约束"],
            exclude=["其他分支的专业结论", "最终全局结论"],
            priority=8,
            parallel_group="specialists",
        )

