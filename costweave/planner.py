from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .domain import Plan, RunRequest, TaskContract
from .model_catalog import SNAPSHOT_DATE


TYPE_RULES: dict[str, tuple[str, ...]] = {
    "coding": ("代码", "开发", "程序", "软件", "接口", "api", "bug", "系统", "网站", "应用", "部署", "架构"),
    "research": ("研究", "调研", "资料", "市场", "竞品", "来源", "事实", "搜索", "最新", "当前", "新闻"),
    "data": ("数据", "统计", "指标", "计算", "报表", "预测", "趋势", "可视化", "数据库"),
    "math": ("数学", "推导", "证明", "公式", "算法", "优化", "概率"),
    "writing": ("文章", "报告", "方案", "文案", "总结", "撰写", "演讲", "邮件", "介绍"),
    "strategy": ("产品", "商业", "战略", "路线图", "需求", "用户", "定位", "决策", "可行性"),
    "translation": ("翻译", "双语", "英文", "中文", "本地化"),
    "vision": ("图片", "图像", "截图", "照片", "视觉", "视频", "看图"),
    "safety": ("安全", "漏洞", "攻击", "隐私", "合规", "法律", "医疗", "金融", "审计"),
}

AMBIGUOUS_WORDS = ("任意", "所有", "全能", "最好", "完善", "等等", "尽量", "智能", "高级")
FRESH_WORDS = ("最新", "现在", "当前", "今日", "价格", "新闻", "政策", "法规", "市场", "竞品", "搜索")
HIGH_STAKES = ("法律", "医疗", "诊断", "投资", "金融", "安全", "漏洞", "隐私", "合规", "生产环境")


@dataclass(slots=True)
class TaskAnalysis:
    task_types: list[str]
    difficulty: int
    ambiguity: float
    uncertainty: float
    stakes: str
    needs_fresh_data: bool
    needs_tools: list[str]
    modalities: list[str]
    decomposition_score: float
    estimate_confidence: float
    risk_signals: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


class HeuristicPlanner:
    """Local-first decision planner with explicit uncertainty and escalation gates."""

    def analyze(self, request: RunRequest) -> TaskAnalysis:
        goal = request.goal.lower()
        task_types = [kind for kind, words in TYPE_RULES.items() if any(word in goal for word in words)]
        if not task_types:
            task_types = ["analysis", "writing"]

        separators = len(re.findall(r"[，,；;、\n]", goal))
        ambiguity_hits = sum(word in goal for word in AMBIGUOUS_WORDS)
        constraint_hits = sum(word in goal for word in ("必须", "不能", "至少", "预算", "期限", "格式", "准确", "验证"))
        needs_fresh_data = any(word in goal for word in FRESH_WORDS)
        high_stakes_hits = sum(word in goal for word in HIGH_STAKES)
        cross_domain = max(0, len(task_types) - 1)

        ambiguity = min(1.0, ambiguity_hits * .18 + (0.2 if len(goal) < 18 else 0))
        uncertainty = min(1.0, ambiguity * .55 + cross_domain * .07 + (0.18 if needs_fresh_data else 0))
        raw = (
            1.5 + len(goal) / 110 + separators * .22 + cross_domain * .75
            + ambiguity_hits * .55 + constraint_hits * .18 + high_stakes_hits * .9
            + (0.55 if needs_fresh_data else 0)
        )
        difficulty = max(1, min(10, round(raw)))
        stakes = "high" if high_stakes_hits else "elevated" if difficulty >= 8 else "normal"

        tools: list[str] = []
        if needs_fresh_data or "research" in task_types:
            tools.append("web_search")
        if any(kind in task_types for kind in ("coding", "data", "math")):
            tools.append("code_execution")
        if "coding" in task_types:
            tools.append("function_calling")

        modalities = ["text"]
        if "vision" in task_types:
            modalities.append("image")

        risk_signals: list[str] = []
        if ambiguity:
            risk_signals.append("目标含宽泛或含糊表达，需先固定范围和验收口径")
        if needs_fresh_data:
            risk_signals.append("结论依赖时效信息，执行模型必须具备检索能力并保留来源")
        if cross_domain >= 2:
            risk_signals.append("跨领域分支可能产生术语、假设或指标冲突")
        if high_stakes_hits:
            risk_signals.append("任务包含高风险领域信号，必须增加独立安全复核")
        if request.budget < .25:
            risk_signals.append("预算偏低，路由器可能无法同时满足质量门槛和成本约束")
        if not risk_signals:
            risk_signals.append("当前执行器为模拟器，不能验证真实领域事实")

        estimate_confidence = max(.45, min(.96, .95 - uncertainty * .35 - (0.08 if stakes == "high" else 0)))
        decomposition_score = min(.98, .52 + len(task_types) * .08 + separators * .025)
        return TaskAnalysis(
            task_types=task_types,
            difficulty=difficulty,
            ambiguity=round(ambiguity, 3),
            uncertainty=round(uncertainty, 3),
            stakes=stakes,
            needs_fresh_data=needs_fresh_data,
            needs_tools=tools,
            modalities=modalities,
            decomposition_score=round(decomposition_score, 3),
            estimate_confidence=round(estimate_confidence, 3),
            risk_signals=risk_signals,
        )

    def plan(self, request: RunRequest) -> Plan:
        analysis = self.analyze(request)
        tasks: list[TaskContract] = [self._alignment(request, analysis)]
        planning_gate = "goal-alignment"

        if analysis.difficulty >= 8 or analysis.estimate_confidence < .68:
            review = self._planning_escalation(request, analysis)
            tasks.append(review)
            planning_gate = review.id

        branch_ids: list[str] = []
        verifier_ids: list[str] = []
        for task_type in analysis.task_types:
            branch = self._branch(task_type, request, analysis, planning_gate)
            if branch.id in branch_ids:
                continue
            tasks.append(branch)
            branch_ids.append(branch.id)
            verifier = self._verification(branch, analysis)
            tasks.append(verifier)
            verifier_ids.append(verifier.id)

        safety_id: str | None = None
        if analysis.stakes == "high" or "safety" in analysis.task_types:
            safety = self._safety_review(verifier_ids, analysis)
            tasks.append(safety)
            safety_id = safety.id

        review_dependencies = [*verifier_ids, *([safety_id] if safety_id else [])]
        tasks.append(TaskContract(
            id="risk-review",
            title="跨分支冲突与风险审查",
            objective="比较所有已验收分支，定位事实冲突、口径不一致、证据缺口和不可接受风险。",
            task_type="risk",
            dependencies=review_dependencies,
            required_capabilities=["risk", "validation", "analysis"],
            acceptance_criteria=["逐项列出冲突", "说明证据强弱", "给出进入汇总或退回重做的判定"],
            output_schema=["summary", "findings", "evidence", "decision", "confidence"],
            include=["验证节点结论", "失败影响范围", "未解决的不确定性"],
            exclude=["代替最终汇总", "隐去相互矛盾的证据"],
            priority=9,
            parallel_group="review",
            difficulty=min(10, analysis.difficulty + 1),
            risk_level=analysis.stakes,
            estimated_input_tokens=12_000 + len(verifier_ids) * 4_000,
            estimated_output_tokens=4_000,
        ))

        tasks.append(TaskContract(
            id="final-synthesis",
            title="证据约束的最终成果整合",
            objective=f"围绕“{request.goal}”整合通过验收的成果，形成一致、可追溯且满足原始约束的交付。",
            task_type="synthesis",
            dependencies=["risk-review"],
            required_capabilities=["synthesis", "writing", "structure", "validation"],
            acceptance_criteria=["覆盖原始目标", "每项关键结论可追溯", "显式保留风险与不确定性", "不引入未验收内容"],
            output_schema=["summary", "deliverable", "evidence", "assumptions", "confidence"],
            include=["通过验收的成果", "风险审查结论", "用户约束"],
            exclude=["被拒绝或失效的中间成果", "无来源的新事实"],
            priority=10,
            parallel_group="synthesis",
            difficulty=min(10, analysis.difficulty + 1),
            risk_level=analysis.stakes,
            estimated_input_tokens=16_000 + len(verifier_ids) * 5_000,
            estimated_output_tokens=6_000 + analysis.difficulty * 500,
        ))

        for task in tasks:
            task.handoff_prompt = self._handoff(task, request)

        base_success = .96 - analysis.difficulty * .028 - analysis.uncertainty * .12
        return Plan(
            version=2,
            difficulty=analysis.difficulty,
            task_types=analysis.task_types,
            rationale=(
                f"识别 {len(analysis.task_types)} 类需求；先完成约束对齐，"
                f"再启动 {len(branch_ids)} 个专业分支和 {len(verifier_ids)} 个独立验收节点，"
                f"最后进行冲突审查与证据约束汇总。"
            ),
            tasks=tasks,
            predicted_success=max(.45, min(.95, base_success)),
            estimate_confidence=analysis.estimate_confidence,
            risks=analysis.risk_signals,
            analysis=analysis.to_dict(),
            model_snapshot=SNAPSHOT_DATE,
        )

    def replan(self, plan: Plan, failed_task_id: str) -> TaskContract:
        plan.version += 1
        replacement_id = f"{failed_task_id}-recovery-v{plan.version}"
        failed = next(task for task in plan.tasks if task.id == failed_task_id)
        replacement = TaskContract(
            id=replacement_id,
            title=f"高级重构：{failed.title}",
            objective=f"根据失败证据重新执行“{failed.objective}”，纠正原有假设并保留独立可验证成果。",
            task_type=failed.task_type,
            dependencies=failed.dependencies.copy(),
            required_capabilities=list(dict.fromkeys([*failed.required_capabilities, "planning", "risk"])),
            acceptance_criteria=[*failed.acceptance_criteria, "解释原方案失败原因", "证明新方案与下游合同兼容"],
            output_schema=failed.output_schema.copy(),
            include=[*failed.include, "失败证据", "已验证的独立成果"],
            exclude=failed.exclude.copy(),
            priority=10,
            parallel_group="recovery",
            difficulty=min(10, failed.difficulty + 2),
            risk_level="high",
            required_modalities=failed.required_modalities.copy(),
            requires_tools=failed.requires_tools.copy(),
            requires_freshness=failed.requires_freshness,
            estimated_input_tokens=round(failed.estimated_input_tokens * 1.35),
            estimated_output_tokens=round(failed.estimated_output_tokens * 1.2),
            plan_version=plan.version,
        )
        replacement.handoff_prompt = self._handoff(replacement, RunRequest(goal=failed.objective))
        for task in plan.tasks:
            task.dependencies = [replacement_id if dep == failed_task_id else dep for dep in task.dependencies]
        plan.tasks.append(replacement)
        plan.predicted_success = min(.95, plan.predicted_success + .04)
        plan.risks.append(f"计划 v{plan.version} 已用高级模型候选替换失败节点 {failed_task_id}")
        return replacement

    @staticmethod
    def _alignment(request: RunRequest, analysis: TaskAnalysis) -> TaskContract:
        return TaskContract(
            id="goal-alignment", title="目标、约束与验收口径对齐",
            objective=f"将用户目标“{request.goal}”转换为明确范围、约束、成功指标和不可做事项。",
            task_type="analysis", required_capabilities=["analysis", "structure", "planning"],
            acceptance_criteria=["保留核心目标", "区分硬约束和偏好", "形成可测试的成功条件", "标注缺失信息"],
            output_schema=["summary", "requirements", "constraints", "open_questions", "evidence", "confidence"],
            include=["目标", "预算", "质量/速度偏好", "风险信号"], exclude=["开始领域执行"],
            priority=10, parallel_group="alignment", difficulty=max(2, analysis.difficulty - 2),
            risk_level=analysis.stakes, estimated_input_tokens=5_000 + len(request.goal) * 8,
            estimated_output_tokens=2_500,
        )

    @staticmethod
    def _planning_escalation(request: RunRequest, analysis: TaskAnalysis) -> TaskContract:
        return TaskContract(
            id="planning-escalation", title="高级模型复核任务图",
            objective=f"复核本地规划器对“{request.goal}”的难度、边界、依赖关系和分工是否可靠。",
            task_type="planning", dependencies=["goal-alignment"],
            required_capabilities=["planning", "analysis", "risk", "long_context"],
            acceptance_criteria=["确认或修正难度", "检查遗漏分支", "验证并行边界", "给出继续执行判定"],
            output_schema=["summary", "plan_adjustments", "risks", "evidence", "confidence"],
            include=["本地任务画像", "目标对齐结果"], exclude=["直接完成全部专业工作"],
            priority=10, parallel_group="planning-gate", difficulty=min(10, analysis.difficulty + 1),
            risk_level=analysis.stakes, estimated_input_tokens=12_000, estimated_output_tokens=4_000,
        )

    @staticmethod
    def _branch(task_type: str, request: RunRequest, analysis: TaskAnalysis, dependency: str) -> TaskContract:
        specs = {
            "coding": ("技术架构与实现", "设计模块、接口、数据流、异常策略和可执行验证方案。", ["coding", "planning", "testing", "tool_use"]),
            "research": ("时效证据研究", "检索并交叉核对事实，记录来源时间、可信度与矛盾。", ["research", "analysis", "validation", "tool_use"]),
            "data": ("数据与指标分析", "定义指标口径、计算路径、敏感性和可复现验证方法。", ["data", "analysis", "math", "validation"]),
            "math": ("定量推理与优化", "建立变量、假设、推导和边界条件，并检查数值稳定性。", ["math", "analysis", "validation"]),
            "writing": ("信息架构与表达", "设计读者导向的内容结构、论证顺序和表达约束。", ["writing", "analysis", "structure"]),
            "strategy": ("产品与决策分析", "比较用户价值、可行性、成本、风险和阶段路线。", ["planning", "analysis", "risk", "synthesis"]),
            "translation": ("跨语言转换", "保持术语、语气、格式与事实含义一致。", ["translation", "writing", "validation"]),
            "vision": ("视觉材料理解", "提取图像中的结构、文字、关系和不确定项。", ["vision", "analysis", "validation"]),
            "safety": ("安全与合规分析", "识别威胁、误用、隐私与合规边界。", ["safety", "risk", "validation"]),
            "analysis": ("核心问题分析", "识别关键决策、假设、因果关系和可行路径。", ["analysis", "planning"]),
        }
        title, objective, capabilities = specs[task_type]
        tools: list[str] = []
        if task_type == "research" or analysis.needs_fresh_data:
            tools.append("web_search")
        if task_type in {"coding", "data", "math"}:
            tools.append("code_execution")
        modalities = ["text", "image"] if task_type == "vision" else ["text"]
        return TaskContract(
            id=f"branch-{task_type}", title=title,
            objective=f"针对“{request.goal}”：{objective}", task_type=task_type,
            dependencies=[dependency], required_capabilities=capabilities,
            acceptance_criteria=["满足任务合同 Schema", "区分事实/推断/假设", "关键结论附证据", "不越出分配范围"],
            output_schema=["summary", "deliverable", "evidence", "assumptions", "confidence"],
            include=[f"{task_type} 范围", "上游已确认约束"], exclude=["其他分支专业结论", "最终全局结论"],
            priority=8, parallel_group="specialists", difficulty=analysis.difficulty,
            risk_level=analysis.stakes, required_modalities=modalities, requires_tools=tools,
            requires_freshness=task_type == "research" and analysis.needs_fresh_data,
            estimated_input_tokens=8_000 + analysis.difficulty * 2_000,
            estimated_output_tokens=2_500 + analysis.difficulty * 550,
        )

    @staticmethod
    def _verification(branch: TaskContract, analysis: TaskAnalysis) -> TaskContract:
        return TaskContract(
            id=f"verify-{branch.task_type}", title=f"独立验收：{branch.title}",
            objective=f"在不复述原答案的前提下，按合同逐项验证 {branch.title} 的完整性、证据和逻辑。",
            task_type="validation", dependencies=[branch.id],
            required_capabilities=["validation", "risk", *branch.required_capabilities[:1]],
            acceptance_criteria=["逐条对照验收标准", "尝试反驳关键结论", "给出通过/退回决定"],
            output_schema=["summary", "checks", "findings", "evidence", "decision", "confidence"],
            include=["分支成果", "任务合同", "证据列表"], exclude=["无理由相信上游结论"],
            priority=9, parallel_group="verification", difficulty=min(10, branch.difficulty + 1),
            risk_level=analysis.stakes, required_modalities=branch.required_modalities.copy(),
            requires_tools=branch.requires_tools.copy() if branch.requires_freshness else [],
            requires_freshness=branch.requires_freshness,
            estimated_input_tokens=branch.estimated_output_tokens + 5_000,
            estimated_output_tokens=2_500,
        )

    @staticmethod
    def _safety_review(verifier_ids: list[str], analysis: TaskAnalysis) -> TaskContract:
        return TaskContract(
            id="domain-safety-review", title="高风险领域独立复核",
            objective="对法律、医疗、金融、安全或隐私相关结论执行保守复核，并指出必须由人类确认的部分。",
            task_type="safety", dependencies=verifier_ids.copy(),
            required_capabilities=["safety", "risk", "validation", "analysis"],
            acceptance_criteria=["识别潜在伤害", "标注不确定性", "给出人工复核边界", "拒绝无证据保证"],
            output_schema=["summary", "hazards", "human_review", "evidence", "decision", "confidence"],
            include=["所有高风险结论"], exclude=["替代持证专业意见"], priority=10,
            parallel_group="safety", difficulty=min(10, analysis.difficulty + 2), risk_level="high",
            estimated_input_tokens=16_000, estimated_output_tokens=4_000,
        )

    @staticmethod
    def _handoff(task: TaskContract, request: RunRequest) -> str:
        return (
            f"你负责子任务：{task.title}\n"
            f"总目标：{request.goal}\n"
            f"你的目标：{task.objective}\n"
            f"只包含：{'；'.join(task.include) or '合同范围'}\n"
            f"明确排除：{'；'.join(task.exclude) or '无'}\n"
            f"验收标准：{'；'.join(task.acceptance_criteria)}\n"
            f"必须输出字段：{', '.join(task.output_schema)}\n"
            "请区分事实、推断和假设；无法验证时明确说明，不得伪造证据。"
        )

