from __future__ import annotations

from dataclasses import dataclass

from .domain import RunMode, TaskContract, WorkerProfile


WORKERS: tuple[WorkerProfile, ...] = (
    WorkerProfile(
        "rule-core", "规则核心", "结构检查与确定性处理",
        {"analysis": .72, "validation": .92, "structure": .98, "planning": .55},
        .004, .35, .97,
    ),
    WorkerProfile(
        "research-scout", "研究侦察员", "证据搜集与资料梳理（模拟）",
        {"research": .91, "analysis": .78, "validation": .72, "writing": .62},
        .025, .85, .88,
    ),
    WorkerProfile(
        "solution-architect", "方案架构师", "系统设计与任务规划（模拟）",
        {"planning": .93, "analysis": .89, "coding": .75, "risk": .78},
        .045, 1.10, .91,
    ),
    WorkerProfile(
        "code-specialist", "实现工程师", "代码实现与技术验证（模拟）",
        {"coding": .95, "testing": .90, "structure": .80, "analysis": .76},
        .040, 1.00, .90,
    ),
    WorkerProfile(
        "data-analyst", "数据分析师", "计算、指标与数据解释（模拟）",
        {"data": .95, "analysis": .88, "validation": .82, "research": .65},
        .032, .95, .90,
    ),
    WorkerProfile(
        "risk-reviewer", "风险审查员", "矛盾识别与独立复核（模拟）",
        {"risk": .96, "validation": .94, "analysis": .84, "planning": .70},
        .030, .90, .93,
    ),
    WorkerProfile(
        "synthesis-editor", "综合编辑", "成果拼接与一致性整理（模拟）",
        {"writing": .94, "synthesis": .96, "analysis": .80, "structure": .84},
        .028, .80, .91,
    ),
    WorkerProfile(
        "senior-planner", "高级规划顾问", "困难规划与全局重构（模拟）",
        {"planning": .98, "analysis": .96, "risk": .92, "synthesis": .88},
        .095, 1.45, .96,
    ),
)


@dataclass(slots=True)
class RouteDecision:
    worker: WorkerProfile
    predicted_success: float
    utility: float
    rationale: str


class PredictiveRouter:
    """Selects a worker before execution using capability/cost/latency estimates."""

    def __init__(self, workers: tuple[WorkerProfile, ...] = WORKERS):
        self.workers = workers

    def route(self, task: TaskContract, mode: RunMode, quality_floor: float) -> RouteDecision:
        candidates: list[RouteDecision] = []
        for worker in self.workers:
            scores = [worker.capabilities.get(cap, .25) for cap in task.required_capabilities]
            capability = sum(scores) / max(len(scores), 1)
            success = min(.995, capability * .72 + worker.reliability * .28)

            if mode == RunMode.ECONOMY:
                cost_weight, latency_weight = 1.65, .25
            elif mode == RunMode.TURBO:
                cost_weight, latency_weight = .28, 1.45
            else:
                cost_weight, latency_weight = .90, .75

            quality_penalty = max(0.0, quality_floor - success) * 2.8
            utility = success - worker.cost_per_task * cost_weight - worker.latency_factor * .04 * latency_weight - quality_penalty
            candidates.append(RouteDecision(
                worker=worker,
                predicted_success=success,
                utility=utility,
                rationale=f"能力匹配 {capability:.0%}，历史可靠性 {worker.reliability:.0%}",
            ))

        viable = [item for item in candidates if item.predicted_success >= quality_floor]
        pool = viable or candidates
        return max(pool, key=lambda item: item.utility)

    def assign(self, tasks: list[TaskContract], mode: RunMode, quality_floor: float) -> None:
        for task in tasks:
            decision = self.route(task, mode, quality_floor)
            task.selected_worker = decision.worker.id
            task.predicted_success = round(decision.predicted_success, 4)
            task.estimated_cost = decision.worker.cost_per_task
            task.estimated_latency_ms = round(520 * decision.worker.latency_factor)

    def catalog(self) -> list[dict]:
        return [worker.to_dict() for worker in self.workers]

    def get_worker(self, worker_id: str) -> WorkerProfile:
        return next(worker for worker in self.workers if worker.id == worker_id)

