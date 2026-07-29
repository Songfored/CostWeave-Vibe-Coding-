"""Rule-first, confidence-aware planning governor for CostWeave v0.5.2."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Iterable

from .domain import RunRequest, WorkerProfile
from .local_classifier import (
    HeuristicDifficultyClassifier,
    LightweightTaskClassifier,
)
from .planner import TaskAnalysis
from .planning_contracts import (
    ExpectedLoss,
    ManagementLimits,
    PanelPolicy,
    PlanningBudget,
    PlanningCandidate,
    PlanningDecision,
    PlanningMode,
    TaskDifficultyVector,
)
from .role_registry import RoleRegistry


def _clamp(value: float, lower: float = 0.0, upper: float = 1.0) -> float:
    return max(lower, min(upper, float(value)))


@dataclass(frozen=True, slots=True)
class CapacityAssessment:
    local_candidates: int
    local_reasoning_ceiling: float
    concurrency_pressure: float
    deadline_pressure: float
    requires_remote_help: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "local_candidates": self.local_candidates,
            "local_reasoning_ceiling": round(
                self.local_reasoning_ceiling, 4
            ),
            "concurrency_pressure": round(
                self.concurrency_pressure, 4
            ),
            "deadline_pressure": round(self.deadline_pressure, 4),
            "requires_remote_help": self.requires_remote_help,
            "reasons": list(self.reasons),
        }


class PlanningGovernor:
    """Chooses a planning mode by hard gates then minimum expected loss."""

    def __init__(
        self,
        *,
        classifier: LightweightTaskClassifier | None = None,
        role_registry: RoleRegistry | None = None,
    ) -> None:
        self.classifier = classifier or HeuristicDifficultyClassifier()
        self.roles = role_registry or RoleRegistry()
        self.limits = ManagementLimits()

    def decide(
        self,
        request: RunRequest,
        analysis: TaskAnalysis,
        workers: Iterable[WorkerProfile],
    ) -> PlanningDecision:
        worker_list = list(workers)
        difficulty = self.classifier.classify(request, analysis)
        capacity = self._capacity(
            request, difficulty, worker_list
        )
        candidates = self._candidates(
            request, analysis, difficulty, worker_list
        )
        candidates, vod, margin = self._apply_vod_gate(
            candidates, request, difficulty
        )
        escalation = self._escalation(
            request, analysis, difficulty, capacity
        )
        selected = self._select(
            candidates, analysis, difficulty, escalation
        )
        role_ids = self._roles_for(selected.mode)
        assignment_policy = self._assignment_policy(
            selected.mode,
            request,
            difficulty,
        )
        assignments = self.roles.assign(
            role_ids,
            worker_list,
            prefer_diversity=selected.mode == PlanningMode.MULTI_PLANNER,
            **assignment_policy,
        )
        budget = PlanningBudget(
            max_cost=selected.planning_cost,
            max_latency_ms=selected.planning_latency_ms,
            max_calls=len(role_ids),
            task_budget_ratio=(
                selected.planning_cost / request.budget
                if request.budget > 0
                else 0.0
            ),
        )
        decision_id = self._decision_id(
            request, selected, difficulty, worker_list
        )
        reasons = self._reasons(
            selected, difficulty, vod, margin, escalation
        )
        rejected = {
            item.mode.value: self._rejection_text(item)
            for item in candidates
            if item.mode != selected.mode
        }
        escalation_payload = {
            **escalation,
            "capacity": capacity.to_dict(),
        }
        return PlanningDecision(
            decision_id=decision_id,
            mode=selected.mode,
            confidence=_clamp(
                difficulty.confidence
                - (
                    0.04
                    if selected.mode == PlanningMode.MULTI_PLANNER
                    else 0.0
                )
            ),
            reasons=tuple(reasons),
            rejected_modes=rejected,
            planning_budget=budget,
            panel_policy=PanelPolicy(
                size=len(role_ids),
                required_diversity=(
                    ("provider", "deployment", "evaluation_focus")
                    if selected.mode == PlanningMode.MULTI_PLANNER
                    else ()
                ),
                selected_roles=tuple(role_ids),
                selected_assignments=tuple(
                    assignment.to_dict()
                    for assignment in assignments
                ),
            ),
            stop_conditions=(
                "达到规划成本、时延或调用硬上限",
                "最佳可行路线相对次优路线的预期损失优势已超过决策边际",
                "MultiPlanner 的 VoD 未超过收益边际时立即停止扩展讨论",
                "一次裁决后不得新增管理层",
            ),
            escalation_conditions=(
                "难度区间上界越过已选路线能力边界",
                "执行中发现高风险、不可逆或未授权范围",
                "本地容量预计无法在期限内提供所需并发",
                "验收证据使成功率下界跌破质量门槛",
            ),
            limits=self.limits,
            difficulty_vector=difficulty,
            candidates=tuple(candidates),
            value_of_deliberation=vod,
            vod_margin=margin,
            pre_execution_escalation=escalation_payload,
            simulated=False,
        )

    def _candidates(
        self,
        request: RunRequest,
        analysis: TaskAnalysis,
        difficulty: TaskDifficultyVector,
        workers: list[WorkerProfile],
    ) -> list[PlanningCandidate]:
        modes = (
            PlanningMode.NO_DELEGATION,
            PlanningMode.DIRECT_DELEGATION,
            PlanningMode.SINGLE_PLANNER,
            PlanningMode.MULTI_PLANNER,
            PlanningMode.EXPERT_OVERRIDE,
            PlanningMode.ASK_USER,
        )
        return [
            self._candidate(
                mode, request, analysis, difficulty, workers
            )
            for mode in modes
        ]

    def _candidate(
        self,
        mode: PlanningMode,
        request: RunRequest,
        analysis: TaskAnalysis,
        difficulty: TaskDifficultyVector,
        workers: list[WorkerProfile],
    ) -> PlanningCandidate:
        d = difficulty.overall.expected
        dims = difficulty.dimensions
        base = (
            0.96
            - d * 0.42
            - dims["ambiguity"] * 0.12
            - dims["verification_difficulty"] * 0.08
        )
        success_bonus = {
            PlanningMode.NO_DELEGATION: -0.04,
            PlanningMode.DIRECT_DELEGATION: 0.025,
            PlanningMode.SINGLE_PLANNER: (
                0.025
                + d * 0.11
                + dims["decomposition_uncertainty"] * 0.035
            ),
            PlanningMode.MULTI_PLANNER: (
                0.04
                + d * 0.1
                + dims["cross_domain_coupling"] * 0.12
                + dims["ambiguity"] * 0.04
            ),
            PlanningMode.EXPERT_OVERRIDE: (
                0.06
                + d * 0.12
                + dims["domain_specialization"] * 0.08
                + dims["error_loss"] * 0.07
            ),
            PlanningMode.ASK_USER: 0.14 + dims["ambiguity"] * 0.12,
        }[mode]
        success = _clamp(base + success_bonus, 0.05, 0.98)
        uncertainty_penalty = (
            (1 - difficulty.confidence) * 0.18
            + (
                difficulty.overall.upper
                - difficulty.overall.expected
            )
            * 0.24
        )
        lower = _clamp(success - uncertainty_penalty, 0.03, success)
        planning_ratio = {
            PlanningMode.NO_DELEGATION: 0.0,
            PlanningMode.DIRECT_DELEGATION: 0.035,
            PlanningMode.SINGLE_PLANNER: 0.08,
            PlanningMode.MULTI_PLANNER: (
                0.18 if analysis.stakes == "high" else 0.14
            ),
            PlanningMode.EXPERT_OVERRIDE: (
                0.17 if analysis.stakes == "high" else 0.11
            ),
            PlanningMode.ASK_USER: 0.0,
        }[mode]
        planning_cost = request.budget * planning_ratio
        execution_ratio = {
            PlanningMode.NO_DELEGATION: 0.08,
            PlanningMode.DIRECT_DELEGATION: 0.15,
            PlanningMode.SINGLE_PLANNER: 0.19,
            PlanningMode.MULTI_PLANNER: 0.24,
            PlanningMode.EXPERT_OVERRIDE: 0.3,
            PlanningMode.ASK_USER: 0.01,
        }[mode]
        execution_cost = request.budget * (
            execution_ratio + d * 0.08
        )
        planning_latency = {
            PlanningMode.NO_DELEGATION: 0,
            PlanningMode.DIRECT_DELEGATION: 900,
            PlanningMode.SINGLE_PLANNER: 2_200,
            PlanningMode.MULTI_PLANNER: 4_800,
            PlanningMode.EXPERT_OVERRIDE: 4_000,
            PlanningMode.ASK_USER: 0,
        }[mode]
        execution_latency = int(
            500
            + d * 4_500
            + {
                PlanningMode.NO_DELEGATION: 0,
                PlanningMode.DIRECT_DELEGATION: 150,
                PlanningMode.SINGLE_PLANNER: 450,
                PlanningMode.MULTI_PLANNER: 700,
                PlanningMode.EXPERT_OVERRIDE: 900,
                PlanningMode.ASK_USER: 0,
            }[mode]
        )
        rejections = self._hard_rejections(
            mode,
            request,
            analysis,
            difficulty,
            workers,
            planning_cost,
            execution_cost,
            lower,
        )
        failure_value = request.budget * (
            1.0 + dims["error_loss"] * 3.2
        )
        value_of_time = (
            request.value_of_time_per_second
            if request.value_of_time_per_second is not None
            else max(0.00001, request.budget / 4_000)
        )
        coordination = request.budget * {
            PlanningMode.NO_DELEGATION: 0.0,
            PlanningMode.DIRECT_DELEGATION: 0.006,
            PlanningMode.SINGLE_PLANNER: 0.012,
            PlanningMode.MULTI_PLANNER: 0.045,
            PlanningMode.EXPERT_OVERRIDE: 0.018,
            PlanningMode.ASK_USER: 0.008,
        }[mode]
        loss = ExpectedLoss(
            money=planning_cost + execution_cost,
            time=(
                planning_latency + execution_latency
            ) / 1000 * value_of_time,
            failure=(1 - success) * failure_value,
            error_risk=(
                (1 - lower)
                * dims["error_loss"]
                * request.budget
                * 1.35
            ),
            coordination=coordination,
        )
        return PlanningCandidate(
            mode=mode,
            feasible=not rejections,
            expected_success=success,
            success_lower_bound=lower,
            expected_cost=planning_cost + execution_cost,
            expected_latency_ms=planning_latency + execution_latency,
            planning_cost=planning_cost,
            planning_latency_ms=planning_latency,
            expected_loss=loss,
            hard_rejections=tuple(rejections),
        )

    @staticmethod
    def _hard_rejections(
        mode: PlanningMode,
        request: RunRequest,
        analysis: TaskAnalysis,
        difficulty: TaskDifficultyVector,
        workers: list[WorkerProfile],
        planning_cost: float,
        execution_cost: float,
        success_lower: float,
    ) -> list[str]:
        d = difficulty.overall
        dims = difficulty.dimensions
        reasons: list[str] = []
        if mode == PlanningMode.NO_DELEGATION and (
            d.upper > 0.34
            or dims["error_loss"] > 0.45
            or dims["verification_difficulty"] > 0.42
        ):
            reasons.append("难度或错误损失超过主线程直接处理边界")
        if mode == PlanningMode.DIRECT_DELEGATION and (
            d.upper > 0.48
            or analysis.stakes != "normal"
            or "safety" in analysis.task_types
            or difficulty.confidence < 0.7
        ):
            reasons.append("不满足低风险、高置信、边界明确的直接委派条件")
        if mode == PlanningMode.SINGLE_PLANNER and (
            analysis.stakes == "high"
            and (
                dims["error_loss"] > 0.75
                or dims["cross_domain_coupling"] > 0.48
            )
        ):
            reasons.append("高损失跨域任务不能只依赖单一规划视角")
        if mode == PlanningMode.MULTI_PLANNER:
            if d.upper < 0.42 and dims["error_loss"] < 0.5:
                reasons.append("任务过于简单，多规划协调开销没有必要")
            if planning_cost > execution_cost:
                reasons.append("规划成本高于预计执行成本")
        if mode == PlanningMode.EXPERT_OVERRIDE:
            remote = [
                worker
                for worker in workers
                if worker.routable and not worker.local
            ]
            if not remote:
                reasons.append("目录中没有可用的远程高级候选")
            if (
                analysis.stakes == "normal"
                and d.upper < 0.62
                and dims["domain_specialization"] < 0.6
                and difficulty.confidence >= 0.68
            ):
                reasons.append(
                    "未触发高风险、低置信、专业能力缺口或难度越界条件"
                )
        if mode == PlanningMode.ASK_USER and not (
            dims["ambiguity"] >= 0.55
            and difficulty.confidence < 0.68
        ):
            reasons.append("现有目标足以形成计划，无需阻塞等待澄清")
        quality_gate = max(0.55, request.quality_floor - 0.13)
        if (
            mode not in {
                PlanningMode.ASK_USER,
                PlanningMode.EXPERT_OVERRIDE,
                PlanningMode.MULTI_PLANNER,
            }
            and success_lower < quality_gate
        ):
            reasons.append(
                f"成功率下界 {success_lower:.0%} 低于规划质量门 {quality_gate:.0%}"
            )
        return reasons

    @staticmethod
    def _apply_vod_gate(
        candidates: list[PlanningCandidate],
        request: RunRequest,
        difficulty: TaskDifficultyVector,
    ) -> tuple[list[PlanningCandidate], float, float]:
        by_mode = {item.mode: item for item in candidates}
        single = by_mode[PlanningMode.SINGLE_PLANNER]
        multi = by_mode[PlanningMode.MULTI_PLANNER]
        vod = max(
            0.0,
            single.expected_loss.total - multi.expected_loss.total,
        )
        margin = max(
            0.005,
            request.budget
            * (
                0.012
                + difficulty.dimensions["cross_domain_coupling"] * 0.012
            ),
        )
        if multi.feasible and vod <= margin:
            replacement = PlanningCandidate(
                mode=multi.mode,
                feasible=False,
                expected_success=multi.expected_success,
                success_lower_bound=multi.success_lower_bound,
                expected_cost=multi.expected_cost,
                expected_latency_ms=multi.expected_latency_ms,
                planning_cost=multi.planning_cost,
                planning_latency_ms=multi.planning_latency_ms,
                expected_loss=multi.expected_loss,
                hard_rejections=(
                    *multi.hard_rejections,
                    f"VoD {vod:.4f} 未超过收益边际 {margin:.4f}",
                ),
            )
            candidates = [
                replacement if item.mode == multi.mode else item
                for item in candidates
            ]
        return candidates, vod, margin

    @staticmethod
    def _capacity(
        request: RunRequest,
        difficulty: TaskDifficultyVector,
        workers: list[WorkerProfile],
    ) -> CapacityAssessment:
        local = [
            worker
            for worker in workers
            if worker.routable
            and worker.local
            and worker.availability >= 0.35
        ]
        ceiling = max(
            (float(worker.reasoning) for worker in local),
            default=0.0,
        )
        concurrency = _clamp(
            request.max_concurrency / max(6, len(local) * 6)
        )
        deadline = (
            _clamp((8 - request.deadline_seconds) / 8)
            if request.deadline_seconds is not None
            else 0.0
        )
        reasons: list[str] = []
        if not local:
            reasons.append("没有可用的本地模型候选")
        if concurrency > 0.85:
            reasons.append("本地并发需求接近或超过保守容量")
        if deadline > 0.5:
            reasons.append("期限过短，本地队列缺少足够余量")
        if difficulty.overall.upper > ceiling + 0.16:
            reasons.append("难度上界超过本地推理能力余量")
        required = bool(reasons) and (
            not local
            or concurrency > 0.85
            or deadline > 0.5
            or (
                difficulty.overall.upper > ceiling + 0.16
                and difficulty.overall.upper > 0.55
            )
        )
        return CapacityAssessment(
            local_candidates=len(local),
            local_reasoning_ceiling=ceiling,
            concurrency_pressure=concurrency,
            deadline_pressure=deadline,
            requires_remote_help=required,
            reasons=tuple(reasons),
        )

    @staticmethod
    def _escalation(
        request: RunRequest,
        analysis: TaskAnalysis,
        difficulty: TaskDifficultyVector,
        capacity: CapacityAssessment,
    ) -> dict[str, object]:
        reasons: list[str] = []
        if difficulty.confidence < 0.68:
            reasons.append("难度判断置信度低于 68%")
        if analysis.estimate_confidence < 0.68:
            reasons.append("任务意图判断置信度低于 68%")
        if analysis.stakes == "high" or "safety" in analysis.task_types:
            reasons.append("高风险任务必须在执行前获得增强规划")
        if capacity.requires_remote_help:
            reasons.extend(capacity.reasons)
        required = bool(reasons)
        return {
            "required": required,
            "target": (
                "MULTI_OR_EXPERT"
                if required
                else "NONE"
            ),
            "reasons": list(dict.fromkeys(reasons)),
            "quality_floor": request.quality_floor,
        }

    @staticmethod
    def _select(
        candidates: list[PlanningCandidate],
        analysis: TaskAnalysis,
        difficulty: TaskDifficultyVector,
        escalation: dict[str, object],
    ) -> PlanningCandidate:
        feasible = [item for item in candidates if item.feasible]
        if not feasible:
            raise RuntimeError("planning governor found no feasible route")
        by_mode = {item.mode: item for item in feasible}
        if (
            PlanningMode.ASK_USER in by_mode
            and difficulty.dimensions["ambiguity"] >= 0.7
        ):
            return by_mode[PlanningMode.ASK_USER]
        if (
            difficulty.overall.upper <= 0.28
            and PlanningMode.NO_DELEGATION in by_mode
        ):
            direct = by_mode[PlanningMode.NO_DELEGATION]
            best = min(
                feasible,
                key=lambda item: (
                    item.expected_loss.total,
                    item.mode.value,
                ),
            )
            if direct.expected_loss.total <= best.expected_loss.total * 1.05:
                return direct
        if bool(escalation["required"]):
            if (
                difficulty.dimensions["cross_domain_coupling"] >= 0.55
                and PlanningMode.MULTI_PLANNER in by_mode
            ):
                return by_mode[PlanningMode.MULTI_PLANNER]
            enhanced = [
                item
                for item in feasible
                if item.mode in {
                    PlanningMode.MULTI_PLANNER,
                    PlanningMode.EXPERT_OVERRIDE,
                }
            ]
            if enhanced:
                return min(
                    enhanced,
                    key=lambda item: (
                        item.expected_loss.total,
                        item.mode.value,
                    ),
                )
        return min(
            feasible,
            key=lambda item: (
                item.expected_loss.total,
                item.mode.value,
            ),
        )

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
    def _assignment_policy(
        mode: PlanningMode,
        request: RunRequest,
        difficulty: TaskDifficultyVector,
    ) -> dict[str, float | bool]:
        upper = difficulty.overall.upper
        quality = request.quality_floor
        if mode == PlanningMode.DIRECT_DELEGATION:
            return {
                "reasoning_floor": min(
                    0.9, max(0.5, quality - 0.15, upper + 0.18)
                ),
                "cost_sensitivity": 0.24,
                "prefer_local": True,
            }
        if mode == PlanningMode.SINGLE_PLANNER:
            return {
                "reasoning_floor": min(
                    0.93, max(0.62, quality - 0.1, upper + 0.15)
                ),
                "cost_sensitivity": 0.18,
                "prefer_local": True,
            }
        if mode == PlanningMode.MULTI_PLANNER:
            return {
                "reasoning_floor": min(
                    0.96, max(0.68, quality - 0.08, upper + 0.08)
                ),
                "cost_sensitivity": 0.1,
                "prefer_local": False,
            }
        if mode == PlanningMode.EXPERT_OVERRIDE:
            return {
                "reasoning_floor": min(
                    0.99, max(0.82, quality - 0.02, upper + 0.1)
                ),
                "cost_sensitivity": 0.04,
                "prefer_local": False,
            }
        return {
            "reasoning_floor": 0.0,
            "cost_sensitivity": 0.0,
            "prefer_local": False,
        }

    @staticmethod
    def _decision_id(
        request: RunRequest,
        selected: PlanningCandidate,
        difficulty: TaskDifficultyVector,
        workers: list[WorkerProfile],
    ) -> str:
        catalog = ",".join(
            sorted(
                f"{worker.id}:{worker.availability:.3f}"
                for worker in workers
                if worker.routable
            )
        )
        raw = "|".join((
            request.goal,
            request.mode.value,
            f"{request.budget:.6f}",
            selected.mode.value,
            f"{difficulty.overall.expected:.6f}",
            f"{difficulty.confidence:.6f}",
            catalog,
        ))
        return "planning_" + hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()[:24]

    @staticmethod
    def _reasons(
        selected: PlanningCandidate,
        difficulty: TaskDifficultyVector,
        vod: float,
        margin: float,
        escalation: dict[str, object],
    ) -> list[str]:
        reasons = [
            (
                f"难度区间 {difficulty.overall.lower:.0%}–"
                f"{difficulty.overall.upper:.0%}，判断置信 "
                f"{difficulty.confidence:.0%}。"
            ),
            (
                f"{selected.mode.value} 的预期总损失最低或通过规则优先门："
                f"{selected.expected_loss.total:.4f}。"
            ),
            (
                f"MultiPlanner VoD {vod:.4f}，收益边际 {margin:.4f}。"
            ),
        ]
        if escalation["required"]:
            reasons.append(
                "执行前升级已触发：" + "；".join(
                    str(item)
                    for item in escalation["reasons"]
                )
            )
        return reasons

    @staticmethod
    def _rejection_text(candidate: PlanningCandidate) -> str:
        if candidate.hard_rejections:
            return "；".join(candidate.hard_rejections)
        return (
            f"可行但预期总损失 {candidate.expected_loss.total:.4f} "
            "高于已选路线。"
        )
