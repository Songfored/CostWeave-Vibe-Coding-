from __future__ import annotations

import math
from dataclasses import dataclass

from .domain import RunMode, TaskContract, WorkerProfile
from .model_catalog import MODELS, catalog_metadata


WORKERS = MODELS


class RoutingError(RuntimeError):
    pass


class BudgetRoutingError(RoutingError):
    pass


@dataclass(slots=True)
class RouteDecision:
    worker: WorkerProfile
    predicted_success: float
    utility: float
    rationale: str
    estimated_cost: float
    estimated_latency_ms: int
    capability_match: float

    def public(self) -> dict:
        return {
            "model_id": self.worker.id,
            "name": self.worker.name,
            "provider": self.worker.provider,
            "predicted_success": round(self.predicted_success, 4),
            "estimated_cost": round(self.estimated_cost, 6),
            "estimated_latency_ms": self.estimated_latency_ms,
            "capability_match": round(self.capability_match, 4),
            "utility": round(self.utility, 4),
            "rationale": self.rationale,
        }


class PredictiveRouter:
    """Constraint-first, multi-objective model routing before execution."""

    def __init__(self, workers: tuple[WorkerProfile, ...] = WORKERS):
        self.workers = workers

    @staticmethod
    def _tool_supported(worker: WorkerProfile, requirement: str) -> bool:
        if requirement in worker.tools:
            return True
        # External code sandboxes can be driven through function calling.
        if requirement == "code_execution" and "function_calling" in worker.tools:
            return True
        return False

    @staticmethod
    def estimate_cost(task: TaskContract, worker: WorkerProfile) -> float:
        if worker.pricing_currency != "USD":
            return math.inf
        return (
            task.estimated_input_tokens / 1_000_000 * worker.input_price_per_mtok
            + task.estimated_output_tokens / 1_000_000 * worker.output_price_per_mtok
        )

    def _rank_candidates(self, task: TaskContract, mode: RunMode) -> list[RouteDecision]:
        candidates: list[RouteDecision] = []
        required_reasoning = min(.98, .54 + task.difficulty * .04)

        for worker in self.workers:
            if not worker.routable:
                continue
            if task.estimated_input_tokens > worker.context_window:
                continue
            if task.estimated_output_tokens > worker.max_output_tokens:
                continue
            if not set(task.required_modalities) <= set(worker.modalities):
                continue
            if any(not self._tool_supported(worker, tool) for tool in task.requires_tools):
                continue
            if task.requires_freshness and "web_search" not in worker.tools:
                continue

            scores = [worker.capabilities.get(cap, .25) for cap in task.required_capabilities]
            minimum = min(scores, default=.5)
            average = sum(scores) / max(len(scores), 1)
            capability = average * .68 + minimum * .32
            if minimum < .42:
                continue

            reasoning_gap = max(0.0, required_reasoning - worker.reasoning)
            tool_fit = 1.0 if task.requires_tools else .88
            context_fit = min(1.0, worker.context_window / max(task.estimated_input_tokens * 4, 1))
            risk_fit = worker.capabilities.get("safety", .6) if task.risk_level == "high" else .88
            success = (
                capability * .43 + worker.reliability * .22 + worker.reasoning * .16
                + tool_fit * .07 + context_fit * .05 + risk_fit * .07
                - reasoning_gap * .52 - (0.025 if worker.preview else 0)
            )
            success = max(.05, min(.995, success))
            cost = self.estimate_cost(task, worker)
            latency = round((180 + task.estimated_output_tokens * .075) * worker.latency_factor)
            cost_score = min(1.0, math.log1p(cost * 45) / math.log(6)) if math.isfinite(cost) else 1.0

            if mode == RunMode.ECONOMY:
                utility = success * .58 + worker.speed * .10 - cost_score * .28 + (.04 if worker.local else 0)
            elif mode == RunMode.TURBO:
                utility = success * .58 + worker.speed * .30 - cost_score * .10
            else:
                utility = success * .72 + worker.speed * .12 - cost_score * .16

            rationale = (
                f"能力 {capability:.0%}；推理 {worker.reasoning:.0%}；可靠性 {worker.reliability:.0%}；"
                f"预计 ${cost:.4f}；{'本地执行' if worker.local else worker.provider}"
            )
            candidates.append(RouteDecision(
                worker=worker,
                predicted_success=success,
                utility=utility,
                rationale=rationale,
                estimated_cost=cost,
                estimated_latency_ms=latency,
                capability_match=capability,
            ))

        return sorted(candidates, key=lambda item: item.utility, reverse=True)

    def route(self, task: TaskContract, mode: RunMode, quality_floor: float) -> RouteDecision:
        ranked = self._rank_candidates(task, mode)
        viable = [item for item in ranked if item.predicted_success >= quality_floor]
        if not viable:
            best = ranked[0].predicted_success if ranked else 0
            raise RoutingError(
                f"质量门槛 {quality_floor:.0%} 无可行模型：{task.title} 的最佳预测仅 {best:.0%}"
            )
        return viable[0]

    @staticmethod
    def _apply(task: TaskContract, selected: RouteDecision, viable: list[RouteDecision]) -> None:
        task.selected_worker = selected.worker.id
        task.predicted_success = round(selected.predicted_success, 4)
        task.estimated_cost = round(selected.estimated_cost, 6)
        task.estimated_latency_ms = selected.estimated_latency_ms
        task.routing_rationale = selected.rationale
        task.routing_candidates = [item.public() for item in viable[:3]]

    def assign(
        self,
        tasks: list[TaskContract],
        mode: RunMode,
        quality_floor: float,
        budget: float | None = None,
    ) -> dict:
        pools: list[tuple[TaskContract, list[RouteDecision]]] = []
        for task in tasks:
            ranked = self._rank_candidates(task, mode)
            viable = [item for item in ranked if item.predicted_success >= quality_floor]
            if not viable:
                best = ranked[0].predicted_success if ranked else 0
                raise RoutingError(
                    f"质量门槛 {quality_floor:.0%} 无可行模型：{task.title} 的最佳预测仅 {best:.0%}"
                )
            pools.append((task, viable))

        # Begin with the cheapest model that clears the hard quality floor.
        choices = [min(pool, key=lambda item: (item.estimated_cost, -item.predicted_success)) for _, pool in pools]
        minimum_cost = sum(item.estimated_cost for item in choices)
        if budget is not None and minimum_cost > budget + 1e-9:
            raise BudgetRoutingError(
                f"预算不足：满足质量门槛至少需要 ${minimum_cost:.4f}，当前预算 ${budget:.4f}"
            )

        # Spend remaining budget only where it buys the most predicted quality or speed.
        if mode != RunMode.ECONOMY:
            ceiling = budget if budget is not None else math.inf
            current_cost = minimum_cost
            target_success = min(.97, quality_floor + (.06 if mode == RunMode.BALANCED else .05))
            minimum_efficiency = .18 if mode == RunMode.BALANCED else .08
            upgrades: list[tuple[float, int, RouteDecision]] = []
            for index, (_, pool) in enumerate(pools):
                base = choices[index]
                for candidate in pool:
                    extra = candidate.estimated_cost - base.estimated_cost
                    if extra <= 1e-9:
                        continue
                    gain = (
                        min(candidate.predicted_success, target_success)
                        - min(base.predicted_success, target_success)
                    )
                    if mode == RunMode.TURBO:
                        gain += max(0, base.worker.speed - candidate.worker.speed) * -.15
                        gain += max(0, candidate.worker.speed - base.worker.speed) * .25
                    efficiency = gain / extra
                    if gain >= .004 and efficiency >= minimum_efficiency:
                        upgrades.append((efficiency, index, candidate))
            for _, index, candidate in sorted(upgrades, reverse=True, key=lambda item: item[0]):
                current = choices[index]
                extra = candidate.estimated_cost - current.estimated_cost
                marginal_gain = (
                    min(candidate.predicted_success, target_success)
                    - min(current.predicted_success, target_success)
                )
                marginal_efficiency = marginal_gain / extra if extra > 1e-9 else 0.0
                if (
                    candidate.predicted_success <= current.predicted_success
                    or marginal_gain < .004
                    or marginal_efficiency < minimum_efficiency
                    or current_cost + extra > ceiling
                ):
                    continue
                choices[index] = candidate
                current_cost += extra

        for (task, viable), selected in zip(pools, choices):
            self._apply(task, selected, viable)

        providers = sorted({item.worker.provider for item in choices})
        return {
            "strategy": "constraint-first multi-objective portfolio",
            "quality_floor": quality_floor,
            "estimated_total_cost_usd": round(sum(item.estimated_cost for item in choices), 6),
            "minimum_feasible_cost_usd": round(minimum_cost, 6),
            "providers": providers,
            "local_assignments": sum(item.worker.local for item in choices),
            "frontier_assignments": sum(item.worker.tier in {"frontier", "premium"} for item in choices),
        }

    def catalog(self) -> list[dict]:
        return [worker.to_dict() for worker in self.workers]

    def catalog_payload(self) -> dict:
        return {"models": self.catalog(), "metadata": catalog_metadata()}

    def get_worker(self, worker_id: str) -> WorkerProfile:
        return next(worker for worker in self.workers if worker.id == worker_id)
