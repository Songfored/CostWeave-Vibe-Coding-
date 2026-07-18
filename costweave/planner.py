from __future__ import annotations

import re
from dataclasses import asdict, dataclass

from .domain import Plan, RunRequest, TaskContract
from .model_catalog import SNAPSHOT_DATE


TAXONOMY_VERSION = "3.0"

# Each label is independently scored. Low-information nouns have lower weights
# than action-object phrases, which avoids treating every occurrence of "API",
# "市场" or "中文" as an executable specialist branch.
TYPE_RULES: dict[str, tuple[tuple[str, float], ...]] = {
    "coding": (
        ("开发", 1.0), ("实现", 1.0), ("编程", 1.0), ("写代码", 1.0),
        ("修复bug", 1.0), ("重构", .9), ("部署", .85), ("技术架构", .85),
        ("接口实现", .85), ("网站", .55), ("应用", .42), ("api", .28), ("系统", .22),
    ),
    "research": (
        ("调研", 1.0), ("检索", 1.0), ("搜索", .95), ("查找", .9),
        ("最新", .8), ("来源", .75), ("竞品", .75), ("事实核查", .9),
        ("新闻", .7), ("资料", .55), ("研究", .5), ("市场", .25),
    ),
    "data": (
        ("数据分析", 1.0), ("统计分析", 1.0), ("数据集", .85), ("指标体系", .8),
        ("可视化", .75), ("数据库", .7), ("报表", .6), ("趋势", .55),
        ("预测模型", .75), ("数据", .35), ("统计", .45), ("计算", .25),
    ),
    "math": (
        ("数学证明", 1.0), ("推导", .95), ("公式", .8), ("概率", .75),
        ("优化算法", .85), ("数值优化", .9), ("定量模型", .7), ("算法", .45),
    ),
    "writing": (
        ("撰写", 1.0), ("写一份", .85), ("文章", .85), ("报告", .75),
        ("文案", .85), ("总结", .7), ("演讲", .75), ("邮件", .7),
        ("介绍", .55), ("方案", .38),
    ),
    "strategy": (
        ("战略", 1.0), ("路线图", .9), ("产品规划", .9), ("商业模式", .9),
        ("定位", .75), ("可行性", .7), ("决策", .65), ("用户需求", .65),
        ("产品", .35), ("商业", .45),
    ),
    "translation": (
        ("翻译", 1.0), ("双语", .9), ("本地化", .85), ("译成", 1.0),
        ("中译英", 1.0), ("英译中", 1.0),
    ),
    "vision": (
        ("看图", 1.0), ("识别图片", 1.0), ("分析截图", 1.0), ("图像理解", .95),
        ("图片", .55), ("图像", .55), ("截图", .6), ("照片", .55),
        ("视觉", .45), ("视频", .5),
    ),
    "safety": (
        ("漏洞", .95), ("攻击路径", 1.0), ("威胁模型", 1.0), ("隐私风险", .85),
        ("合规审查", .9), ("安全审计", 1.0), ("医疗诊断", 1.0),
        ("投资建议", 1.0), ("法律意见", 1.0), ("安全", .42),
        ("隐私", .5), ("合规", .5), ("法律", .45), ("医疗", .45), ("金融", .4),
    ),
}

AMBIGUOUS_WORDS = ("任意", "所有", "全能", "最好", "完善", "等等", "尽量", "智能", "高级")
FRESH_WORDS = ("最新", "现在", "当前", "今日", "价格", "新闻", "政策", "法规", "市场", "竞品", "搜索")
HIGH_STAKES_PATTERNS = (
    "医疗诊断", "症状判断", "药物剂量", "用药建议", "投资建议", "交易建议",
    "投资风险", "法律意见", "合同审查", "漏洞利用", "攻击路径", "生产环境执行",
    "处理个人隐私", "合规结论",
)

INTENT_RULES: dict[str, tuple[str, ...]] = {
    "build": ("开发", "实现", "创建", "制作", "搭建", "生成"),
    "analyze": ("分析", "评估", "比较", "判断", "诊断"),
    "research": ("搜索", "调研", "检索", "查找", "核实"),
    "decide": ("选择", "决策", "推荐", "取舍", "优先级"),
    "transform": ("翻译", "改写", "转换", "整理", "提取"),
    "validate": ("验证", "测试", "验收", "审计", "复核"),
    "troubleshoot": ("修复", "排查", "定位问题", "debug"),
}

DELIVERABLE_RULES: dict[str, tuple[str, ...]] = {
    "code": ("代码", "程序", "应用", "网站", "接口"),
    "report": ("报告", "分析书", "白皮书"),
    "plan": ("方案", "计划", "路线图", "架构"),
    "dataset": ("数据集", "表格", "csv", "数据库"),
    "decision": ("建议", "推荐", "决策", "结论"),
    "translation": ("译文", "翻译", "双语"),
    "visual": ("图片", "图表", "可视化", "视频"),
}


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
    primary_intent: str
    intent_scores: list[dict]
    operations: list[str]
    deliverables: list[str]
    constraints: list[str]
    classifications: list[dict]
    classification_confidence: float
    complexity: dict[str, float]
    clarification_questions: list[str]
    planning_action: str
    taxonomy_version: str = TAXONOMY_VERSION

    def to_dict(self) -> dict:
        return asdict(self)


class HeuristicPlanner:
    """Local-first decision planner with explicit uncertainty and escalation gates."""

    def analyze(self, request: RunRequest) -> TaskAnalysis:
        goal = request.goal.lower()
        classifications: list[dict] = []
        for kind, rules in TYPE_RULES.items():
            evidence = []
            raw_score = 0.0
            for phrase, weight in rules:
                if phrase in goal:
                    evidence.append(phrase)
                    raw_score += weight
            if raw_score:
                score = min(.99, 1 - pow(.48, raw_score))
                classifications.append({
                    "label": kind,
                    "score": round(score, 3),
                    "evidence": evidence,
                    "source": "weighted-local-taxonomy",
                })
        classifications.sort(key=lambda item: (-item["score"], item["label"]))
        task_types = [
            item["label"] for item in classifications
            if item["score"] >= .36
        ]
        if not task_types:
            task_types = ["analysis"]
            classifications.append({
                "label": "analysis",
                "score": .56,
                "evidence": ["无明确专业动作，进入通用分析"],
                "source": "fallback",
            })

        intent_scores: list[dict] = []
        goal_length = max(1, len(goal))
        for operation, words in INTENT_RULES.items():
            matches = [
                (word, goal.find(word))
                for word in words
                if word in goal
            ]
            if not matches:
                continue
            earliest = min(position for _, position in matches)
            position_signal = 1 - earliest / goal_length
            score = min(.99, .48 + len(matches) * .12 + position_signal * .28)
            intent_scores.append({
                "label": operation,
                "score": round(score, 3),
                "evidence": [word for word, _ in matches],
                "earliest_position": earliest,
            })
        intent_scores.sort(
            key=lambda item: (-item["score"], item["earliest_position"], item["label"])
        )
        if not intent_scores:
            intent_scores = [{
                "label": "analyze",
                "score": .52,
                "evidence": ["通用分析回退"],
                "earliest_position": 0,
            }]
        operations = [item["label"] for item in intent_scores]
        primary_intent = operations[0]
        deliverables = [
            artifact for artifact, words in DELIVERABLE_RULES.items()
            if any(word in goal for word in words)
        ] or ["answer"]
        clauses = [
            clause.strip()
            for clause in re.split(r"[。；;\n]", request.goal)
            if clause.strip()
        ]
        constraints = [
            clause for clause in clauses
            if any(word in clause for word in (
                "必须", "不能", "不得", "至少", "最多", "预算", "期限",
                "格式", "准确", "并行", "本地", "开源", "不要",
            ))
        ][:8]

        separators = len(re.findall(r"[，,；;、\n]", goal))
        ambiguity_hits = sum(word in goal for word in AMBIGUOUS_WORDS)
        constraint_hits = sum(word in goal for word in ("必须", "不能", "至少", "预算", "期限", "格式", "准确", "验证"))
        needs_fresh_data = any(word in goal for word in FRESH_WORDS)
        high_stakes_hits = sum(pattern in goal for pattern in HIGH_STAKES_PATTERNS)
        elevated_risk_hits = sum(
            word in goal for word in ("法律", "医疗", "投资", "金融", "安全", "漏洞", "隐私", "合规", "生产环境")
        )
        cross_domain = max(0, len(task_types) - 1)

        top_score = classifications[0]["score"]
        second_score = classifications[1]["score"] if len(classifications) > 1 else 0.0
        margin = max(0.0, top_score - second_score)
        classification_confidence = min(
            .97,
            .48 + top_score * .32 + margin * .28 + min(.1, len(constraints) * .025),
        )
        ambiguity = min(
            1.0,
            ambiguity_hits * .15
            + (0.18 if len(goal) < 12 else 0)
            + (0.12 if margin < .08 and len(classifications) > 1 else 0),
        )

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

        cognitive = min(
            1.0,
            .18
            + sum(word in goal for word in ("推导", "证明", "架构", "优化", "因果", "权衡", "复杂")) * .12
            + len(task_types) * .055,
        )
        coordination = min(1.0, .12 + cross_domain * .14 + separators * .025)
        evidence_complexity = min(
            1.0,
            (.34 if needs_fresh_data else .08)
            + sum(word in goal for word in ("核实", "来源", "交叉验证", "证据")) * .13,
        )
        tool_complexity = min(1.0, len(tools) * .22 + (len(modalities) - 1) * .2)
        domain_complexity = min(1.0, high_stakes_hits * .55 + elevated_risk_hits * .16)
        scope_complexity = min(
            1.0,
            .12 + min(len(goal), 420) / 700 + len(deliverables) * .08 + len(constraints) * .04,
        )
        complexity = {
            "cognitive": round(cognitive, 3),
            "coordination": round(coordination, 3),
            "evidence": round(evidence_complexity, 3),
            "tools": round(tool_complexity, 3),
            "domain": round(domain_complexity, 3),
            "scope": round(scope_complexity, 3),
        }
        complexity_index = (
            cognitive * .27 + coordination * .20 + evidence_complexity * .15
            + tool_complexity * .12 + domain_complexity * .16 + scope_complexity * .10
        )
        difficulty = max(1, min(10, round(1.2 + complexity_index * 8.8)))
        stakes = (
            "high" if high_stakes_hits
            else "elevated" if elevated_risk_hits or difficulty >= 8
            else "normal"
        )
        uncertainty = min(
            1.0,
            ambiguity * .36
            + (1 - classification_confidence) * .32
            + evidence_complexity * .18
            + cross_domain * .035,
        )

        risk_signals: list[str] = []
        if ambiguity:
            risk_signals.append("目标含宽泛或含糊表达，需先固定范围和验收口径")
        if needs_fresh_data:
            risk_signals.append("结论依赖时效信息，执行模型必须具备检索能力并保留来源")
        if cross_domain >= 2:
            risk_signals.append("跨领域分支可能产生术语、假设或指标冲突")
        if high_stakes_hits:
            risk_signals.append("任务包含高风险领域信号，必须增加独立安全复核")
        elif elevated_risk_hits:
            risk_signals.append("任务涉及敏感领域，但未发现直接专业处置请求；采用提升级风险复核")
        if request.budget < .25:
            risk_signals.append("预算偏低，路由器可能无法同时满足质量门槛和成本约束")
        if not risk_signals:
            risk_signals.append("当前执行器为模拟器，不能验证真实领域事实")

        estimate_confidence = max(
            .38,
            min(
                .96,
                classification_confidence * .58
                + (1 - ambiguity) * .24
                + (1 - evidence_complexity) * .10
                + .08,
            ),
        )
        decomposition_score = min(
            .98,
            .48 + len(task_types) * .075 + separators * .02 + len(deliverables) * .035,
        )
        clarification_questions: list[str] = []
        if ambiguity >= .28:
            clarification_questions.append("任务范围、完成边界和不可做事项分别是什么？")
        if not constraints:
            clarification_questions.append("是否有预算、期限、格式或必须满足的质量约束？")
        if needs_fresh_data:
            clarification_questions.append("需要以哪个时间点和地区的资料作为有效基线？")
        planning_action = (
            "senior-review"
            if difficulty >= 8 or estimate_confidence < .68 or stakes == "high"
            else "local-plan"
        )
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
            primary_intent=primary_intent,
            intent_scores=intent_scores,
            operations=operations,
            deliverables=deliverables,
            constraints=constraints,
            classifications=classifications,
            classification_confidence=round(classification_confidence, 3),
            complexity=complexity,
            clarification_questions=clarification_questions,
            planning_action=planning_action,
        )

    def plan(self, request: RunRequest) -> Plan:
        analysis = self.analyze(request)
        tasks: list[TaskContract] = [self._alignment(request, analysis)]
        planning_gate = "goal-alignment"

        if analysis.planning_action == "senior-review":
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
            output_schema=["summary", "findings", "criteria_results", "evidence", "decision", "confidence"],
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
            task.uncertainty = analysis.uncertainty
            task.classification_confidence = analysis.classification_confidence
            if task.id == "final-synthesis":
                task.criticality = 1.0
            elif task.task_type in {"validation", "risk", "safety", "planning"}:
                task.criticality = .92
            elif task.id == "goal-alignment":
                task.criticality = .88
            else:
                task.criticality = .78
            task.capability_weights = {
                capability: (1.35 if index == 0 else 1.0)
                for index, capability in enumerate(task.required_capabilities)
            }
            task.handoff_prompt = self._handoff(task, request)

        base_success = .96 - analysis.difficulty * .028 - analysis.uncertainty * .12
        return Plan(
            version=3,
            difficulty=analysis.difficulty,
            task_types=analysis.task_types,
            rationale=(
                f"分层画像识别主意图 {analysis.primary_intent} 与 {len(analysis.task_types)} 个专业域；"
                f"先完成约束对齐，"
                f"再启动 {len(branch_ids)} 个专业分支和 {len(verifier_ids)} 个独立验收节点，"
                f"最后进行冲突审查与证据约束汇总。"
            ),
            tasks=tasks,
            predicted_success=max(.45, min(.95, base_success)),
            estimate_confidence=analysis.estimate_confidence,
            risks=analysis.risk_signals,
            analysis=analysis.to_dict(),
            model_snapshot=f"builtin-{SNAPSHOT_DATE}",
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
            capability_weights={
                **failed.capability_weights,
                "planning": 1.2,
                "risk": 1.15,
            },
            uncertainty=min(1.0, failed.uncertainty + .12),
            criticality=max(.9, failed.criticality),
            classification_confidence=failed.classification_confidence,
            escalation_policy="failure-cause-upgrade",
            plan_version=plan.version,
        )
        replacement.handoff_prompt = self._handoff(replacement, RunRequest(goal=failed.objective))
        for task in plan.tasks:
            task.dependencies = [replacement_id if dep == failed_task_id else dep for dep in task.dependencies]
        plan.tasks.append(replacement)
        plan.predicted_success = max(.35, plan.predicted_success * .94)
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
            output_schema=["summary", "plan_adjustments", "risks", "criteria_results", "evidence", "decision", "confidence"],
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
        difficulty_offsets = {
            "math": 1,
            "coding": 1,
            "safety": 1,
            "research": 1 if analysis.needs_fresh_data else 0,
            "writing": -1,
            "translation": -1,
        }
        branch_difficulty = max(
            1,
            min(10, analysis.difficulty + difficulty_offsets.get(task_type, 0)),
        )
        return TaskContract(
            id=f"branch-{task_type}", title=title,
            objective=f"针对“{request.goal}”：{objective}", task_type=task_type,
            dependencies=[dependency], required_capabilities=capabilities,
            acceptance_criteria=["满足任务合同 Schema", "区分事实/推断/假设", "关键结论附证据", "不越出分配范围"],
            output_schema=["summary", "deliverable", "evidence", "assumptions", "confidence"],
            include=[f"{task_type} 范围", "上游已确认约束"], exclude=["其他分支专业结论", "最终全局结论"],
            priority=8, parallel_group="specialists", difficulty=branch_difficulty,
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
            output_schema=["summary", "checks", "findings", "criteria_results", "evidence", "decision", "confidence"],
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
            output_schema=["summary", "hazards", "human_review", "criteria_results", "evidence", "decision", "confidence"],
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
