"""Deterministic v0.5.1 planning-mode simulation and proposal receipts."""

from __future__ import annotations

import hashlib
from typing import Iterable

from .domain import RunRequest, WorkerProfile
from .planner import TaskAnalysis
from .planning_contracts import (
    ManagementLimits,
    PanelPolicy,
    PlanProposal,
    PlanningBudget,
    PlanningDecision,
    PlanningMode,
)
from .role_registry import RoleRegistry


class DeliberationSimulator:
    """Freezes v0.5.1 contracts without pretending to be the v0.5.2 governor."""

    def __init__(self, role_registry: RoleRegistry | None = None) -> None:
        self.roles = role_registry or RoleRegistry()
        self.limits = ManagementLimits()

    def decide(
        self,
        request: RunRequest,
        analysis: TaskAnalysis,
        workers: Iterable[WorkerProfile],
    ) -> PlanningDecision:
        mode, reasons = self._mode(analysis)
        role_ids = self._roles_for(mode)
        assignments = self.roles.assign(
            role_ids,
            workers,
            prefer_diversity=mode == PlanningMode.MULTI_PLANNER,
        )
        ratio = self._budget_ratio(mode, analysis.stakes)
        calls = len(role_ids)
        budget = PlanningBudget(
            max_cost=request.budget * ratio,
            max_latency_ms=self._latency_limit(mode, request),
            max_calls=calls,
            task_budget_ratio=ratio,
        )
        decision_key = "|".join((
            request.goal,
            request.mode.value,
            f"{request.budget:.6f}",
            f"{request.quality_floor:.6f}",
            mode.value,
            ",".join(role_ids),
        ))
        decision_id = "planning_" + hashlib.sha256(
            decision_key.encode("utf-8")
        ).hexdigest()[:24]
        return PlanningDecision(
            decision_id=decision_id,
            mode=mode,
            confidence=max(
                0.4,
                min(
                    0.96,
                    float(analysis.estimate_confidence)
                    - (0.05 if mode == PlanningMode.MULTI_PLANNER else 0),
                ),
            ),
            reasons=tuple(reasons),
            rejected_modes=self._rejected(mode, analysis),
            planning_budget=budget,
            panel_policy=PanelPolicy(
                size=len(role_ids),
                required_diversity=(
                    ("provider", "deployment", "evaluation_focus")
                    if mode == PlanningMode.MULTI_PLANNER
                    else ()
                ),
                selected_roles=tuple(role_ids),
                selected_assignments=tuple(
                    assignment.to_dict() for assignment in assignments
                ),
            ),
            stop_conditions=(
                "合同完整且所有硬限制通过即停止规划",
                "达到规划调用、成本或时延上限立即停止",
                "一次裁决后不得继续增加管理层",
            ),
            escalation_conditions=(
                "实际范围触及未授权的高风险或不可逆操作",
                "角色无法形成满足验收标准的提案",
                "规划置信度低于任务质量底线",
            ),
            limits=self.limits,
        )

    def proposals(
        self,
        decision: PlanningDecision,
        request: RunRequest,
        analysis: TaskAnalysis,
    ) -> list[PlanProposal]:
        proposals: list[PlanProposal] = []
        assignments = list(decision.panel_policy.selected_assignments)
        if decision.panel_policy.size == 0:
            return proposals
        per_cost = decision.planning_budget.max_cost / max(
            1, decision.panel_policy.size
        )
        per_latency = decision.planning_budget.max_latency_ms // max(
            1, decision.panel_policy.size
        )
        focus = {
            "domain-planner": (
                ("目标对齐", "按依赖拆分", "逐节点验收"),
                ("领域边界可能被误判",),
                ("依赖闭合", "交付物逐项验收"),
            ),
            "economics-planner": (
                ("识别关键路径", "并行独立分支", "设置预算停止线"),
                ("协调开销可能抵消并行收益",),
                ("核对总成本和 p95 时延", "检查预算硬上限"),
            ),
            "risk-planner": (
                ("标记高损失节点", "设置证据门", "设计局部回滚"),
                ("低置信结果可能被错误拼接",),
                ("关键结论独立复核", "失败时只扩大必要范围"),
            ),
        }
        for index, role_id in enumerate(decision.panel_policy.selected_roles):
            outline, risks, acceptance = focus[role_id]
            assignment = assignments[index] if index < len(assignments) else {}
            proposal_key = (
                f"{decision.decision_id}:{role_id}:{request.goal}"
            )
            proposals.append(PlanProposal(
                proposal_id="proposal_" + hashlib.sha256(
                    proposal_key.encode("utf-8")
                ).hexdigest()[:24],
                planner_role=role_id,
                objective_interpretation=request.goal,
                decomposition_outline=outline,
                assumptions=tuple(analysis.constraints) or (
                    "用户尚未提供额外硬约束，沿用运行预算和质量底线",
                ),
                risks=risks,
                acceptance_strategy=acceptance,
                estimated_planning_cost=per_cost,
                estimated_planning_latency_ms=per_latency,
                confidence=max(
                    0.4,
                    min(0.95, analysis.estimate_confidence - index * 0.02),
                ),
                assignment=assignment,
            ))
        return proposals

    @staticmethod
    def _mode(analysis: TaskAnalysis) -> tuple[PlanningMode, list[str]]:
        if (
            analysis.ambiguity >= 0.6
            and analysis.estimate_confidence < 0.62
        ):
            return PlanningMode.ASK_USER, [
                "目标歧义很高且本地分类置信不足，继续拆解会放大提示偏差。",
                "v0.5.1 只记录询问建议；现有离线演示仍会按兼容规划器运行。",
            ]
        if (
            analysis.stakes == "high"
            or "safety" in analysis.task_types
            or analysis.difficulty >= 9
        ):
            return PlanningMode.MULTI_PLANNER, [
                "任务包含高损失或极高难度信号，单一视角不足以冻结计划。",
                "采用领域、经济和风险三个互补角色，管理深度仍固定为两层。",
            ]
        if analysis.estimate_confidence < 0.6:
            return PlanningMode.EXPERT_OVERRIDE, [
                "本地任务判定置信度低于安全阈值。",
                "执行前应将计划决定权交给一个高级规划角色。",
            ]
        if (
            analysis.difficulty >= 7
            and (
                len(analysis.task_types) >= 2
                or analysis.ambiguity >= 0.25
                or analysis.uncertainty >= 0.3
            )
        ):
            return PlanningMode.MULTI_PLANNER, [
                "任务同时具有较高难度与跨领域或不确定性信号。",
                "多视角提案的预期价值可能覆盖一次受限讨论开销。",
            ]
        if analysis.difficulty <= 2 and analysis.ambiguity < 0.2:
            return PlanningMode.NO_DELEGATION, [
                "任务边界清楚、难度很低，委派开销可能高于直接处理。",
                "保留 NO_DELEGATION，避免为了多智能体而拆分。",
            ]
        if (
            analysis.difficulty <= 4
            and len(analysis.task_types) == 1
            and analysis.classification_confidence >= 0.75
            and analysis.stakes == "normal"
        ):
            return PlanningMode.DIRECT_DELEGATION, [
                "任务类型单一且分类置信较高，不需要单独规划回合。",
                "只委派一个边界明确的执行角色。",
            ]
        return PlanningMode.SINGLE_PLANNER, [
            "任务需要显式规划，但未达到多 Planner 的成本与风险门槛。",
            "一个领域规划角色足以形成任务图。",
        ]

    @staticmethod
    def _roles_for(mode: PlanningMode) -> list[str]:
        return {
            PlanningMode.NO_DELEGATION: [],
            PlanningMode.ASK_USER: [],
            PlanningMode.DIRECT_DELEGATION: ["domain-planner"],
            PlanningMode.SINGLE_PLANNER: ["domain-planner"],
            PlanningMode.MULTI_PLANNER: [
                "domain-planner",
                "economics-planner",
                "risk-planner",
            ],
            PlanningMode.EXPERT_OVERRIDE: ["risk-planner"],
        }[mode]

    @staticmethod
    def _budget_ratio(mode: PlanningMode, stakes: str) -> float:
        ratios = {
            PlanningMode.NO_DELEGATION: 0.0,
            PlanningMode.ASK_USER: 0.0,
            PlanningMode.DIRECT_DELEGATION: 0.05,
            PlanningMode.SINGLE_PLANNER: 0.10,
            PlanningMode.MULTI_PLANNER: 0.15,
            PlanningMode.EXPERT_OVERRIDE: 0.12,
        }
        ratio = ratios[mode]
        if stakes == "high" and mode in {
            PlanningMode.MULTI_PLANNER,
            PlanningMode.EXPERT_OVERRIDE,
        }:
            ratio = min(0.25, ratio + 0.05)
        return ratio

    @staticmethod
    def _latency_limit(
        mode: PlanningMode, request: RunRequest
    ) -> int:
        defaults = {
            PlanningMode.NO_DELEGATION: 0,
            PlanningMode.ASK_USER: 0,
            PlanningMode.DIRECT_DELEGATION: 1_500,
            PlanningMode.SINGLE_PLANNER: 3_000,
            PlanningMode.MULTI_PLANNER: 6_000,
            PlanningMode.EXPERT_OVERRIDE: 5_000,
        }
        limit = defaults[mode]
        if request.deadline_seconds is not None:
            limit = min(limit, int(request.deadline_seconds * 1000 * 0.2))
        return max(0, limit)

    @staticmethod
    def _rejected(
        selected: PlanningMode, analysis: TaskAnalysis
    ) -> dict[str, str]:
        reasons = {
            PlanningMode.NO_DELEGATION: "任务仍需显式规划或委派。",
            PlanningMode.DIRECT_DELEGATION: "任务不满足单一、低风险、边界清楚的直接委派条件。",
            PlanningMode.SINGLE_PLANNER: "单一规划视角与当前难度、风险或歧义不匹配。",
            PlanningMode.MULTI_PLANNER: "额外讨论开销未达到必要门槛。",
            PlanningMode.EXPERT_OVERRIDE: "尚未触发低置信或强制高级复核条件。",
            PlanningMode.ASK_USER: "现有目标足以形成可验收的离线计划。",
        }
        reasons.pop(selected)
        if selected == PlanningMode.MULTI_PLANNER:
            reasons[PlanningMode.SINGLE_PLANNER] = (
                f"难度 L{analysis.difficulty} 且风险/不确定性要求互补视角。"
            )
        return {mode.value: reason for mode, reason in reasons.items()}
