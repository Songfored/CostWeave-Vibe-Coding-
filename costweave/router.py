from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from typing import Any

from .catalog_store import CATALOG_STORE, CatalogStore
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
    success_lower_bound: float
    prediction_uncertainty: float
    utility: float
    rationale: str
    estimated_cost: float
    estimated_latency_ms: int
    capability_match: float
    score_components: dict[str, float]

    def public(self) -> dict[str, Any]:
        return {
            "model_id": self.worker.id,
            "name": self.worker.name,
            "provider": self.worker.provider,
            "predicted_success": round(self.predicted_success, 4),
            "success_lower_bound": round(self.success_lower_bound, 4),
            "prediction_uncertainty": round(self.prediction_uncertainty, 4),
            "estimated_cost": round(self.estimated_cost, 6),
            "estimated_latency_ms": self.estimated_latency_ms,
            "capability_match": round(self.capability_match, 4),
            "utility": round(self.utility, 4),
            "score_components": {
                key: round(value, 4) for key, value in self.score_components.items()
            },
            "rationale": self.rationale,
        }


class PredictiveRouter:
    """Snapshot-aware, constraint-first and uncertainty-aware portfolio router."""

    def __init__(
        self,
        workers: tuple[WorkerProfile, ...] | None = None,
        *,
        store: CatalogStore | None = None,
        catalog_revision: str | None = None,
    ) -> None:
        self._static_workers = workers
        self.store = None if workers is not None else (store or CATALOG_STORE)
        self._catalog_revision = catalog_revision

    @property
    def workers(self) -> tuple[WorkerProfile, ...]:
        if self._static_workers is not None:
            return self._static_workers
        return self.store.snapshot()

    @property
    def catalog_revision(self) -> str:
        if self._catalog_revision:
            return self._catalog_revision
        if self.store is not None:
            payload = self.store.payload()["metadata"]
            return f"r{payload['catalog_revision']}-{payload['catalog_hash']}"
        return "static"

    def freeze(self) -> "PredictiveRouter":
        return PredictiveRouter(
            workers=self.workers,
            catalog_revision=self.catalog_revision,
        )

    @staticmethod
    def _tool_supported(worker: WorkerProfile, requirement: str) -> bool:
        # Function calling is not a code sandbox. Tools are explicit hard facts.
        return requirement in worker.tools

    @staticmethod
    def estimate_cost(task: TaskContract, worker: WorkerProfile) -> float:
        if worker.pricing_currency != "USD":
            return math.inf
        return (
            task.estimated_input_tokens / 1_000_000 * worker.input_price_per_mtok
            + task.estimated_output_tokens / 1_000_000 * worker.output_price_per_mtok
        )

    @staticmethod
    def _freshness_quality(worker: WorkerProfile) -> float:
        if not worker.verified_at:
            return 0.45
        try:
            checked = date.fromisoformat(worker.verified_at)
        except ValueError:
            return 0.35
        age = max(0, (date.today() - checked).days)
        if age <= 45:
            return 1.0
        if age <= 120:
            return 0.88
        if age <= 365:
            return 0.72
        return 0.52

    def _hard_rejections(self, task: TaskContract, worker: WorkerProfile) -> list[str]:
        reasons: list[str] = []
        if not worker.routable:
            reasons.append("已停用路由")
        if worker.pricing_currency != "USD":
            reasons.append("缺少美元汇率快照")
        if task.estimated_input_tokens > worker.context_window:
            reasons.append("上下文不足")
        if task.estimated_output_tokens > worker.max_output_tokens:
            reasons.append("输出上限不足")
        missing_modalities = sorted(set(task.required_modalities) - set(worker.modalities))
        if missing_modalities:
            reasons.append("缺少模态：" + ",".join(missing_modalities))
        missing_tools = [
            tool for tool in task.requires_tools
            if not self._tool_supported(worker, tool)
        ]
        if missing_tools:
            reasons.append("缺少工具：" + ",".join(missing_tools))
        if task.requires_freshness and "web_search" not in worker.tools:
            reasons.append("不具备时效检索")
        scores = [worker.capabilities.get(cap, 0.2) for cap in task.required_capabilities]
        if scores and min(scores) < 0.42:
            reasons.append("关键能力短板")
        return reasons

    def _rank_candidates(
        self,
        task: TaskContract,
        mode: RunMode,
    ) -> tuple[list[RouteDecision], list[dict[str, Any]]]:
        candidates: list[RouteDecision] = []
        rejections: list[dict[str, Any]] = []
        required_reasoning = min(0.99, 0.48 + task.difficulty * 0.047)

        for worker in self.workers:
            reasons = self._hard_rejections(task, worker)
            if reasons:
                rejections.append({
                    "model_id": worker.id,
                    "name": worker.name,
                    "provider": worker.provider,
                    "reasons": reasons,
                })
                continue

            weights = {
                capability: max(0.05, task.capability_weights.get(capability, 1.0))
                for capability in task.required_capabilities
            }
            scores = {
                capability: worker.capabilities.get(capability, 0.25)
                for capability in task.required_capabilities
            }
            total_weight = sum(weights.values()) or 1.0
            weighted_average = sum(
                scores[capability] * weights[capability]
                for capability in scores
            ) / total_weight
            minimum = min(scores.values(), default=0.5)
            capability = weighted_average * 0.72 + minimum * 0.28

            reasoning_gap = max(0.0, required_reasoning - worker.reasoning)
            tool_fit = 1.0 if task.requires_tools else 0.9
            context_fit = min(
                1.0,
                worker.context_window / max(task.estimated_input_tokens * 4, 1),
            )
            risk_fit = (
                worker.capabilities.get("safety", 0.55)
                if task.risk_level == "high"
                else 0.9
            )
            freshness_quality = self._freshness_quality(worker)
            data_quality = min(
                1.0,
                worker.data_confidence * 0.72 + freshness_quality * 0.28,
            )
            reliability = worker.reliability * worker.availability

            mean = (
                capability * 0.40
                + reliability * 0.19
                + worker.reasoning * 0.15
                + tool_fit * 0.07
                + context_fit * 0.05
                + risk_fit * 0.07
                + data_quality * 0.07
                - reasoning_gap * 0.48
                - task.uncertainty * 0.035
                - (0.025 if worker.preview else 0)
            )
            mean = max(0.05, min(0.995, mean))
            uncertainty = (
                0.018
                + (1 - data_quality) * 0.045
                + task.uncertainty * 0.035
                + (1 - task.classification_confidence) * 0.025
                + (0.02 if worker.preview else 0)
                + (0.012 if task.risk_level == "high" else 0)
            )
            lower_bound = max(0.03, mean - uncertainty)

            cost = self.estimate_cost(task, worker)
            latency = round(
                (180 + task.estimated_output_tokens * 0.075)
                * worker.latency_factor
            )
            cost_score = (
                min(1.0, math.log1p(cost * 45) / math.log(6))
                if math.isfinite(cost)
                else 1.0
            )
            criticality = max(0.1, min(1.0, task.criticality))
            if mode == RunMode.ECONOMY:
                utility = (
                    lower_bound * (0.54 + criticality * 0.06)
                    + worker.speed * 0.08
                    - cost_score * 0.28
                    - uncertainty * 0.18
                    + (0.04 if worker.local else 0)
                )
            elif mode == RunMode.TURBO:
                utility = (
                    lower_bound * 0.56
                    + worker.speed * 0.30
                    - cost_score * 0.08
                    - uncertainty * 0.15
                )
            else:
                utility = (
                    lower_bound * (0.66 + criticality * 0.08)
                    + worker.speed * 0.10
                    - cost_score * 0.14
                    - uncertainty * 0.20
                )

            components = {
                "capability": capability,
                "reliability": reliability,
                "reasoning": worker.reasoning,
                "tool_fit": tool_fit,
                "context_fit": context_fit,
                "risk_fit": risk_fit,
                "catalog_quality": data_quality,
            }
            rationale = (
                f"能力 {capability:.0%}；保守成功率 {lower_bound:.0%}；"
                f"目录可信度 {data_quality:.0%}；预计 ${cost:.4f}；"
                f"{'本地执行' if worker.local else worker.provider}"
            )
            candidates.append(RouteDecision(
                worker=worker,
                predicted_success=mean,
                success_lower_bound=lower_bound,
                prediction_uncertainty=uncertainty,
                utility=utility,
                rationale=rationale,
                estimated_cost=cost,
                estimated_latency_ms=latency,
                capability_match=capability,
                score_components=components,
            ))

        candidates.sort(
            key=lambda item: (
                item.utility,
                item.success_lower_bound,
                -item.estimated_cost,
                item.worker.id,
            ),
            reverse=True,
        )
        return candidates, rejections

    def route(
        self,
        task: TaskContract,
        mode: RunMode,
        quality_floor: float,
    ) -> RouteDecision:
        ranked, rejections = self._rank_candidates(task, mode)
        task.routing_rejections = rejections[:8]
        viable = [
            item for item in ranked
            if item.success_lower_bound >= quality_floor
        ]
        if not viable:
            best = max(
                (item.success_lower_bound for item in ranked),
                default=0,
            )
            raise RoutingError(
                f"质量门槛 {quality_floor:.0%} 无可行模型："
                f"{task.title} 的最佳保守预测仅 {best:.0%}"
            )
        return viable[0]

    @staticmethod
    def _apply(
        task: TaskContract,
        selected: RouteDecision,
        viable: list[RouteDecision],
        rejections: list[dict[str, Any]],
    ) -> None:
        task.selected_worker = selected.worker.id
        task.predicted_success = round(selected.predicted_success, 4)
        task.predicted_success_lower_bound = round(
            selected.success_lower_bound, 4
        )
        task.route_confidence = round(
            1 - selected.prediction_uncertainty, 4
        )
        task.estimated_cost = round(selected.estimated_cost, 6)
        task.estimated_latency_ms = selected.estimated_latency_ms
        task.routing_rationale = selected.rationale
        task.routing_candidates = [item.public() for item in viable[:5]]
        task.routing_rejections = rejections[:8]

    @staticmethod
    def _independent_choice(
        task: TaskContract,
        pool: list[RouteDecision],
        producer: RouteDecision | None,
    ) -> RouteDecision:
        cheapest = min(
            pool,
            key=lambda item: (
                item.estimated_cost,
                -item.success_lower_bound,
                item.worker.id,
            ),
        )
        if producer is None or task.task_type != "validation":
            return cheapest
        independent = [
            item for item in pool
            if item.worker.id != producer.worker.id
            and (
                task.risk_level != "high"
                or item.worker.provider != producer.worker.provider
            )
        ]
        if not independent:
            independent = [
                item for item in pool
                if item.worker.id != producer.worker.id
            ]
        if not independent:
            task.routing_rationale = "验收独立性降级：没有其他可行模型"
            return cheapest
        return min(
            independent,
            key=lambda item: (
                item.estimated_cost,
                -item.success_lower_bound,
                item.worker.id,
            ),
        )

    def assign(
        self,
        tasks: list[TaskContract],
        mode: RunMode,
        quality_floor: float,
        budget: float | None = None,
    ) -> dict[str, Any]:
        pools: list[
            tuple[TaskContract, list[RouteDecision], list[dict[str, Any]]]
        ] = []
        for task in tasks:
            ranked, rejections = self._rank_candidates(task, mode)
            task_floor = min(
                0.99,
                quality_floor
                + max(0.0, task.criticality - 0.75) * 0.025
                + (0.01 if task.risk_level == "high" else 0),
            )
            viable = [
                item for item in ranked
                if item.success_lower_bound >= task_floor
            ]
            if not viable:
                best = max(
                    (item.success_lower_bound for item in ranked),
                    default=0,
                )
                reason = ""
                if rejections:
                    reason = "；主要淘汰原因：" + "，".join(
                        rejections[0]["reasons"]
                    )
                raise RoutingError(
                    f"质量门槛 {task_floor:.0%} 无可行模型："
                    f"{task.title} 的最佳保守预测仅 {best:.0%}{reason}"
                )
            pools.append((task, viable, rejections))

        task_by_id = {task.id: task for task in tasks}
        selected_by_task: dict[str, RouteDecision] = {}
        choices: list[RouteDecision] = []
        for task, pool, _ in pools:
            producer = None
            if task.task_type == "validation" and task.dependencies:
                producer = selected_by_task.get(task.dependencies[0])
            selected = self._independent_choice(task, pool, producer)
            choices.append(selected)
            selected_by_task[task.id] = selected

        minimum_cost = sum(item.estimated_cost for item in choices)
        if budget is not None and minimum_cost > budget + 1e-9:
            raise BudgetRoutingError(
                f"预算不足：满足质量与独立验收约束至少需要 "
                f"${minimum_cost:.4f}，当前预算 ${budget:.4f}"
            )

        if mode != RunMode.ECONOMY:
            ceiling = budget if budget is not None else math.inf
            current_cost = minimum_cost
            target_success = min(
                0.97,
                quality_floor
                + (0.055 if mode == RunMode.BALANCED else 0.045),
            )
            minimum_efficiency = (
                0.18 if mode == RunMode.BALANCED else 0.08
            )
            upgrades: list[tuple[float, int, RouteDecision]] = []
            for index, (task, pool, _) in enumerate(pools):
                base = choices[index]
                for candidate in pool:
                    if (
                        task.task_type == "validation"
                        and candidate.worker.id == base.worker.id
                    ):
                        continue
                    extra = candidate.estimated_cost - base.estimated_cost
                    if extra <= 1e-9:
                        continue
                    gain = (
                        min(candidate.success_lower_bound, target_success)
                        - min(base.success_lower_bound, target_success)
                    ) * max(0.5, task.criticality)
                    if mode == RunMode.TURBO:
                        gain += max(
                            0.0,
                            candidate.worker.speed - base.worker.speed,
                        ) * 0.25
                    efficiency = gain / extra
                    if gain >= 0.004 and efficiency >= minimum_efficiency:
                        upgrades.append((efficiency, index, candidate))
            for _, index, candidate in sorted(
                upgrades,
                reverse=True,
                key=lambda item: (item[0], item[2].worker.id),
            ):
                current = choices[index]
                extra = candidate.estimated_cost - current.estimated_cost
                task = pools[index][0]
                marginal_gain = (
                    min(candidate.success_lower_bound, target_success)
                    - min(current.success_lower_bound, target_success)
                ) * max(0.5, task.criticality)
                marginal_efficiency = (
                    marginal_gain / extra if extra > 1e-9 else 0.0
                )
                if (
                    candidate.success_lower_bound
                    <= current.success_lower_bound
                    or marginal_gain < 0.004
                    or marginal_efficiency < minimum_efficiency
                    or current_cost + extra > ceiling
                ):
                    continue
                choices[index] = candidate
                current_cost += extra

        # Portfolio upgrades can accidentally make a validator converge on its
        # producer. Re-check independence after all upgrades.
        choice_index = {
            task.id: index
            for index, (task, _, _) in enumerate(pools)
        }
        ceiling = budget if budget is not None else math.inf
        current_cost = sum(item.estimated_cost for item in choices)
        for index, (task, pool, _) in enumerate(pools):
            if task.task_type != "validation" or not task.dependencies:
                continue
            producer_index = choice_index.get(task.dependencies[0])
            if producer_index is None:
                continue
            producer = choices[producer_index]
            current = choices[index]
            independent = (
                current.worker.id != producer.worker.id
                and (
                    task.risk_level != "high"
                    or current.worker.provider != producer.worker.provider
                )
            )
            if independent:
                continue
            alternatives = [
                item for item in pool
                if item.worker.id != producer.worker.id
                and (
                    task.risk_level != "high"
                    or item.worker.provider != producer.worker.provider
                )
            ]
            if not alternatives:
                alternatives = [
                    item for item in pool
                    if item.worker.id != producer.worker.id
                ]
            if not alternatives:
                continue
            alternative = min(
                alternatives,
                key=lambda item: (
                    item.estimated_cost,
                    -item.success_lower_bound,
                    item.worker.id,
                ),
            )
            delta = alternative.estimated_cost - current.estimated_cost
            if current_cost + delta <= ceiling + 1e-9:
                choices[index] = alternative
                current_cost += delta

        for (task, viable, rejections), selected in zip(pools, choices):
            self._apply(task, selected, viable, rejections)

        providers = sorted({item.worker.provider for item in choices})
        independent_validators = 0
        degraded_validators = 0
        selected_map = {
            task.id: choice for (task, _, _), choice in zip(pools, choices)
        }
        for task, _, _ in pools:
            if task.task_type != "validation" or not task.dependencies:
                continue
            producer = selected_map.get(task.dependencies[0])
            validator = selected_map.get(task.id)
            if producer and validator and producer.worker.id != validator.worker.id:
                independent_validators += 1
            else:
                degraded_validators += 1
                task.routing_rationale = (
                    task.routing_rationale
                    + "；验收独立性降级：没有满足预算与质量门槛的独立候选"
                )

        return {
            "strategy": "risk-adjusted constraint portfolio v3",
            "quality_floor": quality_floor,
            "quality_metric": "conservative lower bound",
            "estimated_total_cost_usd": round(
                sum(item.estimated_cost for item in choices), 6
            ),
            "minimum_feasible_cost_usd": round(minimum_cost, 6),
            "providers": providers,
            "local_assignments": sum(item.worker.local for item in choices),
            "frontier_assignments": sum(
                item.worker.tier in {"frontier", "premium"}
                for item in choices
            ),
            "independent_validators": independent_validators,
            "independence_degraded": degraded_validators,
            "catalog_revision": self.catalog_revision,
        }

    def catalog(self) -> list[dict[str, Any]]:
        return [worker.to_dict() for worker in self.workers]

    def catalog_payload(self) -> dict[str, Any]:
        if self.store is not None:
            return self.store.payload()
        return {
            "models": self.catalog(),
            "metadata": {
                **catalog_metadata(),
                "catalog_revision": self.catalog_revision,
                "editable": False,
            },
        }

    def get_worker(self, worker_id: str) -> WorkerProfile:
        try:
            return next(
                worker for worker in self.workers
                if worker.id == worker_id
            )
        except StopIteration as exc:
            raise RoutingError(f"目录快照中不存在模型：{worker_id}") from exc
