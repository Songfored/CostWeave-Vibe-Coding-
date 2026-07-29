"""Explainable model/provider/pool/profile/start-window optimization."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from .cross_provider import (
    CROSS_PROVIDER_POLICY_REVISION,
    CrossProviderPolicy,
    PosteriorLookup,
)
from .domain import RunMode, TaskContract
from .inference_policy import InferencePolicy
from .placements import PlacementCandidate, placement_id
from .resource_economics import ResourceEconomics
from .resource_inventory import ResourcePool


class PlacementError(RuntimeError):
    pass


class PlacementBudgetError(PlacementError):
    pass


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    selected: dict[str, PlacementCandidate]
    backups: dict[str, tuple[PlacementCandidate, ...]]
    summary: dict[str, Any]


class ResourcePortfolioOptimizer:
    def __init__(
        self,
        *,
        economics: ResourceEconomics | None = None,
        posterior_lookup: PosteriorLookup | None = None,
        switch_minimum_usd: float = 0.00001,
        switch_minimum_ratio: float = 0.03,
    ) -> None:
        self.economics = economics or ResourceEconomics()
        self.cross_provider = CrossProviderPolicy(posterior_lookup)
        self.switch_minimum_usd = switch_minimum_usd
        self.switch_minimum_ratio = switch_minimum_ratio

    def candidates(
        self,
        *,
        task: TaskContract,
        routes: list[Any],
        pools: list[ResourcePool],
        quality_floor: float,
        availability: dict[str, dict[str, Any]] | None = None,
        deadline_ms: int | None = None,
        value_of_time_per_second: float | None = None,
        admission_audit: list[dict[str, Any]] | None = None,
    ) -> list[PlacementCandidate]:
        availability = availability or {}
        candidates: list[PlacementCandidate] = []
        for route in routes:
            worker = route.worker
            matching = [
                pool for pool in pools
                if pool.provider == worker.provider
                and pool.local == worker.local
            ]
            for pool in matching:
                if (
                    task.resource_slots > pool.slots_total
                    or (
                        pool.cpu_cores_total > 0
                        and task.cpu_cores > pool.cpu_cores_total
                    )
                    or task.gpu_count > pool.gpu_count_total
                    or task.vram_mb > pool.vram_mb_total
                ):
                    continue
                available = availability.get(
                    pool.pool_id,
                    {
                        "slots": pool.slots_total,
                        "queue_depth": 0,
                    },
                )
                for profile in InferencePolicy.candidates(worker, task):
                    assessment = self.cross_provider.assess(
                        task=task,
                        worker=worker,
                        pool=pool,
                        profile=profile,
                        prior_mean=max(
                            0.03,
                            min(
                                0.997,
                                route.predicted_success
                                + profile.quality_delta,
                            ),
                        ),
                        prior_lower=max(
                            0.03,
                            min(
                                0.995,
                                route.success_lower_bound
                                + profile.quality_delta,
                            ),
                        ),
                        prior_uncertainty=route.prediction_uncertainty,
                        base_latency_ms=route.estimated_latency_ms,
                        availability=available,
                        deadline_ms=deadline_ms,
                    )
                    assessment_data = assessment.to_dict()
                    if admission_audit is not None:
                        admission_audit.append(
                            {
                                "task_id": task.id,
                                "worker_id": worker.id,
                                **assessment_data,
                            }
                        )
                    if not assessment.admitted:
                        continue
                    lower = assessment.success_lower_bound
                    mean = assessment.predicted_success
                    if lower + 1e-12 < quality_floor:
                        continue
                    execution_ms = assessment.execution_p50_ms
                    queue_depth = max(
                        0, int(available.get("queue_depth", 0))
                    )
                    queue_ms = round(
                        queue_depth
                        / max(1, pool.slots_total)
                        * execution_ms
                    )
                    finish_p95 = assessment.finish_p95_ms
                    forecast = self.economics.forecast(
                        task=task,
                        worker=worker,
                        pool=pool,
                        profile=profile,
                        direct_cost=route.estimated_cost,
                        success_lower_bound=lower,
                        execution_ms=execution_ms,
                        queue_ms=queue_ms,
                        available_slots=int(
                            available.get("slots", pool.slots_total)
                        ),
                        value_of_time_per_second=(
                            value_of_time_per_second
                        ),
                    )
                    identifier = placement_id(
                        task.id,
                        worker.id,
                        pool.pool_id,
                        profile.revision,
                        task.plan_version,
                    )
                    candidates.append(
                        PlacementCandidate(
                            placement_id=identifier,
                            task_id=task.id,
                            plan_revision=task.plan_version,
                            model_id=worker.id,
                            model_revision=(
                                f"{worker.provider}/"
                                f"{worker.model_id or worker.id}"
                            ),
                            provider=worker.provider,
                            resource_pool=pool.pool_id,
                            inference_profile=profile.to_dict(),
                            predicted_quality_mean=mean,
                            predicted_quality_lower=lower,
                            predicted_start_p50_ms=queue_ms,
                            predicted_finish_p95_ms=finish_p95,
                            execution_p50_ms=execution_ms,
                            direct_model_cost_usd=(
                                route.estimated_cost
                                * profile.token_multiplier
                                if not worker.local
                                else 0.0
                            ),
                            expected_total_cost_usd=forecast.p50.total,
                            forecast_cost=forecast.to_dict(),
                            reservation={
                                "slots": max(1, task.resource_slots),
                                "cpu_cores": max(0.0, task.cpu_cores),
                                "gpu_count": max(0, task.gpu_count),
                                "gpu_memory_mb": max(0, task.vram_mb),
                                "duration_p95_ms": finish_p95 - queue_ms,
                            },
                            deployment=assessment.deployment.to_dict(),
                            posterior={
                                "level": assessment.posterior_level,
                                "samples": assessment.posterior_samples,
                                "cold_start": assessment.cold_start,
                                "catalog_freshness": (
                                    assessment.catalog_freshness
                                ),
                                "predicted_success": mean,
                                "success_lower_bound": lower,
                                "uncertainty": assessment.uncertainty,
                            },
                            admission={
                                "admitted": True,
                                "code": assessment.admission_code,
                                "reasons": [],
                                "capacity": assessment.capacity,
                                "policy_revision": (
                                    CROSS_PROVIDER_POLICY_REVISION
                                ),
                            },
                            latency_forecast={
                                "execution_p50_ms": execution_ms,
                                "finish_p95_ms": finish_p95,
                                "deadline_miss_probability": (
                                    assessment.deadline_miss_probability
                                ),
                            },
                            rationale=(
                                f"质量下界 {lower:.0%}；"
                                f"端到端预期成本 "
                                f"${forecast.p50.total:.5f}；"
                                f"直接调用 ${route.estimated_cost:.5f}；"
                                f"排队 {queue_ms}ms；"
                                f"{profile.reasoning_effort} 推理"
                            ),
                        )
                    )
        return sorted(
            candidates,
            key=lambda item: (
                item.expected_total_cost_usd,
                item.predicted_finish_p95_ms,
                -item.predicted_quality_lower,
                item.placement_id,
            ),
        )

    def optimize(
        self,
        *,
        tasks: list[TaskContract],
        route_pools: dict[str, list[Any]],
        pools: list[ResourcePool],
        quality_floor: float,
        mode: RunMode,
        budget: float | None,
        availability: dict[str, dict[str, Any]] | None = None,
        deadline_ms: int | None = None,
        value_of_time_per_second: float | None = None,
    ) -> OptimizationResult:
        all_candidates: dict[str, list[PlacementCandidate]] = {}
        admission_audit: list[dict[str, Any]] = []
        for task in tasks:
            capacity_match = any(
                pool.provider == route.worker.provider
                and pool.local == route.worker.local
                and task.resource_slots <= pool.slots_total
                and (
                    pool.cpu_cores_total <= 0
                    or task.cpu_cores <= pool.cpu_cores_total
                )
                and task.gpu_count <= pool.gpu_count_total
                and task.vram_mb <= pool.vram_mb_total
                for route in route_pools.get(task.id, [])
                for pool in pools
            )
            if not capacity_match:
                raise PlacementError(
                    f"impossible_capacity: no resource pool can satisfy "
                    f"{task.title}"
                )
            task_floor = min(
                0.99,
                quality_floor
                + max(0.0, task.criticality - 0.75) * 0.025
                + (0.01 if task.risk_level == "high" else 0),
            )
            task_candidates = self.candidates(
                task=task,
                routes=route_pools.get(task.id, []),
                pools=pools,
                quality_floor=task_floor,
                availability=availability,
                deadline_ms=deadline_ms,
                value_of_time_per_second=value_of_time_per_second,
                admission_audit=admission_audit,
            )
            if not task_candidates:
                raise PlacementError(
                    f"no feasible placement for {task.title}: "
                    "quality, deadline or capacity policy rejected all candidates"
                )
            all_candidates[task.id] = task_candidates

        selected = {
            task.id: self._mode_choice(
                all_candidates[task.id], mode
            )
            for task in tasks
        }
        self._repair_validator_independence(
            tasks, all_candidates, selected
        )
        if deadline_ms is not None:
            self._repair_deadline(
                tasks,
                all_candidates,
                selected,
                deadline_ms,
            )
            self._repair_validator_independence(
                tasks, all_candidates, selected
            )
        budget_rebalances = 0
        if budget is not None:
            budget_rebalances = self._repair_budget(
                tasks=tasks,
                candidates=all_candidates,
                selected=selected,
                budget=budget,
                mode=mode,
                deadline_ms=deadline_ms,
            )
        total = sum(
            item.expected_total_cost_usd
            for item in selected.values()
        )
        if budget is not None and total > budget + 1e-9:
            raise PlacementBudgetError(
                f"placement portfolio requires ${total:.4f}, "
                f"budget allows ${budget:.4f}"
            )

        backups = {
            task.id: tuple(
                item
                for item in all_candidates[task.id]
                if item.placement_id
                != selected[task.id].placement_id
            )[:2]
            for task in tasks
        }
        critical_path_ms = self._critical_path_ms(tasks, selected)
        if (
            deadline_ms is not None
            and critical_path_ms > deadline_ms
        ):
            raise PlacementError(
                f"fastest feasible placement portfolio p95 "
                f"{critical_path_ms}ms exceeds deadline {deadline_ms}ms"
            )
        direct_total = sum(
            item.direct_model_cost_usd
            for item in selected.values()
        )
        selected_routes = [
            {
                "task_id": task.id,
                "model_id": selected[task.id].model_id,
                "provider": selected[task.id].provider,
                "deployment": selected[task.id].deployment,
                "reasoning_effort": selected[task.id].inference_profile[
                    "reasoning_effort"
                ],
                "predicted_success": round(
                    selected[task.id].predicted_quality_mean, 6
                ),
                "success_lower_bound": round(
                    selected[task.id].predicted_quality_lower, 6
                ),
                "expected_total_cost_usd": round(
                    selected[task.id].expected_total_cost_usd, 8
                ),
                "direct_model_cost_usd": round(
                    selected[task.id].direct_model_cost_usd, 8
                ),
                "expected_rework_usd": round(
                    float(
                        selected[task.id].forecast_cost["p50"].get(
                            "expected_rework", 0.0
                        )
                    ),
                    8,
                ),
                "finish_p95_ms": selected[
                    task.id
                ].predicted_finish_p95_ms,
                "deadline_miss_probability": selected[
                    task.id
                ].latency_forecast["deadline_miss_probability"],
                "admission": selected[task.id].admission,
                "posterior": selected[task.id].posterior,
            }
            for task in tasks
        ]
        rejected_assessments = [
            item for item in admission_audit
            if not item["admitted"]
        ]
        return OptimizationResult(
            selected=selected,
            backups=backups,
            summary={
                "strategy": (
                    "cross-provider expected-value portfolio v2"
                ),
                "policy_revision": CROSS_PROVIDER_POLICY_REVISION,
                "estimated_total_cost_usd": round(total, 6),
                "direct_model_cost_usd": round(direct_total, 6),
                "non_model_cost_usd": round(
                    max(0.0, total - direct_total), 6
                ),
                "critical_path_p95_ms": critical_path_ms,
                "placement_candidates": sum(
                    len(items) for items in all_candidates.values()
                ),
                "backup_placements": sum(
                    len(items) for items in backups.values()
                ),
                "budget_rebalances": budget_rebalances,
                "local_assignments": sum(
                    item.provider == "local"
                    for item in selected.values()
                ),
                "cross_provider": {
                    "schema_version": "v0.5.4",
                    "policy_revision": (
                        CROSS_PROVIDER_POLICY_REVISION
                    ),
                    "pre_execution_admission": True,
                    "assessments": len(admission_audit),
                    "admitted_candidates": sum(
                        bool(item["admitted"])
                        for item in admission_audit
                    ),
                    "diverted_or_rejected": len(
                        rejected_assessments
                    ),
                    "cold_start_assessments": sum(
                        bool(item["cold_start"])
                        for item in admission_audit
                    ),
                    "selected_routes": selected_routes,
                    "rejections": rejected_assessments[:24],
                    "expected_rework_usd": round(
                        sum(
                            float(
                                item.forecast_cost["p50"].get(
                                    "expected_rework", 0.0
                                )
                            )
                            for item in selected.values()
                        ),
                        8,
                    ),
                },
            },
        )

    @staticmethod
    def _mode_choice(
        candidates: list[PlacementCandidate],
        mode: RunMode,
    ) -> PlacementCandidate:
        if mode == RunMode.TURBO:
            return min(
                candidates,
                key=lambda item: (
                    item.predicted_finish_p95_ms,
                    item.expected_total_cost_usd,
                    -item.predicted_quality_lower,
                ),
            )
        if mode == RunMode.BALANCED:
            cheapest = candidates[0]
            frontier = [
                item for item in candidates
                if item.expected_total_cost_usd
                <= cheapest.expected_total_cost_usd * 1.15 + 0.00002
            ]
            return min(
                frontier,
                key=lambda item: (
                    -item.predicted_quality_lower,
                    item.expected_total_cost_usd,
                    item.predicted_finish_p95_ms,
                ),
            )
        if mode == RunMode.QUALITY:
            return min(
                candidates,
                key=lambda item: (
                    -item.predicted_quality_lower,
                    item.expected_total_cost_usd,
                    item.predicted_finish_p95_ms,
                ),
            )
        return candidates[0]

    @staticmethod
    def _repair_validator_independence(
        tasks: list[TaskContract],
        candidates: dict[str, list[PlacementCandidate]],
        selected: dict[str, PlacementCandidate],
    ) -> None:
        for task in tasks:
            if task.task_type != "validation" or not task.dependencies:
                continue
            producer = selected.get(task.dependencies[0])
            current = selected[task.id]
            if producer is None or (
                current.model_id != producer.model_id
                and (
                    task.risk_level != "high"
                    or current.provider != producer.provider
                )
            ):
                continue
            alternatives = [
                item for item in candidates[task.id]
                if item.model_id != producer.model_id
                and (
                    task.risk_level != "high"
                    or item.provider != producer.provider
                )
            ]
            if not alternatives:
                alternatives = [
                    item for item in candidates[task.id]
                    if item.model_id != producer.model_id
                ]
            if alternatives:
                selected[task.id] = alternatives[0]

    @classmethod
    def _repair_budget(
        cls,
        *,
        tasks: list[TaskContract],
        candidates: dict[str, list[PlacementCandidate]],
        selected: dict[str, PlacementCandidate],
        budget: float,
        mode: RunMode,
        deadline_ms: int | None,
    ) -> int:
        """Find a lower-cost feasible portfolio before rejecting a run.

        Initial mode selection is intentionally unconstrained so Quality and
        Turbo can express their ideal portfolio. This pass then walks the
        cost/quality/latency frontier, preserving validator independence and
        the deadline, until the hard placement budget is met.
        """

        task_weights = {
            task.id: max(0.25, task.criticality)
            for task in tasks
        }
        total = sum(
            item.expected_total_cost_usd
            for item in selected.values()
        )
        changes = 0
        maximum_changes = max(
            1, sum(len(items) for items in candidates.values()) * 2
        )
        while total > budget + 1e-9 and changes < maximum_changes:
            replacements: list[
                tuple[
                    float,
                    float,
                    float,
                    str,
                    dict[str, PlacementCandidate],
                ]
            ] = []
            for task in tasks:
                current = selected[task.id]
                for candidate in candidates[task.id]:
                    if candidate.placement_id == current.placement_id:
                        continue
                    trial = dict(selected)
                    trial[task.id] = candidate
                    cls._repair_validator_independence(
                        tasks, candidates, trial
                    )
                    if not cls._validator_portfolio_is_independent(
                        tasks, candidates, trial
                    ):
                        continue
                    if (
                        deadline_ms is not None
                        and cls._critical_path_ms(tasks, trial)
                        > deadline_ms
                    ):
                        continue
                    trial_total = sum(
                        item.expected_total_cost_usd
                        for item in trial.values()
                    )
                    saving = total - trial_total
                    if saving <= 1e-12:
                        continue
                    penalty = cls._portfolio_downgrade_penalty(
                        tasks=tasks,
                        current=selected,
                        replacement=trial,
                        mode=mode,
                        task_weights=task_weights,
                    )
                    replacements.append(
                        (
                            penalty / saving,
                            penalty,
                            -saving,
                            task.id,
                            trial,
                        )
                    )
            if not replacements:
                break
            _, _, _, _, replacement = min(replacements)
            selected.clear()
            selected.update(replacement)
            total = sum(
                item.expected_total_cost_usd
                for item in selected.values()
            )
            changes += 1
        return changes

    @staticmethod
    def _portfolio_downgrade_penalty(
        *,
        tasks: list[TaskContract],
        current: dict[str, PlacementCandidate],
        replacement: dict[str, PlacementCandidate],
        mode: RunMode,
        task_weights: dict[str, float],
    ) -> float:
        penalty = 0.0
        for task in tasks:
            before = current[task.id]
            after = replacement[task.id]
            if before.placement_id == after.placement_id:
                continue
            weight = task_weights[task.id]
            quality_loss = max(
                0.0,
                before.predicted_quality_lower
                - after.predicted_quality_lower,
            )
            latency_loss = max(
                0.0,
                after.predicted_finish_p95_ms
                - before.predicted_finish_p95_ms,
            ) / max(1_000, before.predicted_finish_p95_ms)
            if mode == RunMode.QUALITY:
                component = quality_loss + latency_loss * 0.02
            elif mode == RunMode.TURBO:
                component = latency_loss + quality_loss * 0.15
            elif mode == RunMode.BALANCED:
                component = quality_loss + latency_loss * 0.25
            else:
                component = quality_loss * 0.05
            penalty += component * weight
        return penalty

    @staticmethod
    def _validator_portfolio_is_independent(
        tasks: list[TaskContract],
        candidates: dict[str, list[PlacementCandidate]],
        selected: dict[str, PlacementCandidate],
    ) -> bool:
        for task in tasks:
            if task.task_type != "validation" or not task.dependencies:
                continue
            producer = selected.get(task.dependencies[0])
            validator = selected.get(task.id)
            if producer is None or validator is None:
                continue
            if validator.model_id == producer.model_id:
                return False
            if (
                task.risk_level == "high"
                and validator.provider == producer.provider
                and any(
                    item.model_id != producer.model_id
                    and item.provider != producer.provider
                    for item in candidates[task.id]
                )
            ):
                return False
        return True

    @classmethod
    def _repair_deadline(
        cls,
        tasks: list[TaskContract],
        candidates: dict[str, list[PlacementCandidate]],
        selected: dict[str, PlacementCandidate],
        deadline_ms: int,
    ) -> None:
        while cls._critical_path_ms(tasks, selected) > deadline_ms:
            replacements: list[
                tuple[float, float, str, PlacementCandidate]
            ] = []
            for task in tasks:
                current = selected[task.id]
                for candidate in candidates[task.id]:
                    reduction = (
                        current.predicted_finish_p95_ms
                        - candidate.predicted_finish_p95_ms
                    )
                    if reduction <= 0:
                        continue
                    extra = max(
                        0.0,
                        candidate.expected_total_cost_usd
                        - current.expected_total_cost_usd,
                    )
                    replacements.append(
                        (
                            reduction / max(extra, 0.000001),
                            reduction,
                            task.id,
                            candidate,
                        )
                    )
            if not replacements:
                return
            _, _, task_id, replacement = max(
                replacements,
                key=lambda item: (
                    item[0], item[1], -item[3].expected_total_cost_usd
                ),
            )
            selected[task_id] = replacement

    @staticmethod
    def _critical_path_ms(
        tasks: list[TaskContract],
        selected: dict[str, PlacementCandidate],
    ) -> int:
        task_ids = {task.id for task in tasks}
        remaining = {
            task.id: sum(
                dependency in task_ids
                for dependency in task.dependencies
            )
            for task in tasks
        }
        children = {task.id: [] for task in tasks}
        for task in tasks:
            for dependency in task.dependencies:
                if dependency in task_ids:
                    children[dependency].append(task.id)
        queue = [task.id for task in tasks if remaining[task.id] == 0]
        finish: dict[str, int] = {}
        while queue:
            current = queue.pop(0)
            task = next(item for item in tasks if item.id == current)
            start = max(
                (
                    finish[dependency]
                    for dependency in task.dependencies
                    if dependency in task_ids
                ),
                default=0,
            )
            finish[current] = (
                start + selected[current].predicted_finish_p95_ms
            )
            for child in children.get(current, []):
                remaining[child] -= 1
                if remaining[child] == 0:
                    queue.append(child)
        if len(finish) != len(tasks):
            raise PlacementError("placement optimizer received cyclic DAG")
        return max(finish.values(), default=0)

    def should_switch(
        self,
        current: PlacementCandidate,
        replacement: PlacementCandidate,
    ) -> bool:
        saving = (
            current.expected_total_cost_usd
            - replacement.expected_total_cost_usd
        )
        ratio = saving / max(
            current.expected_total_cost_usd, 1e-12
        )
        return (
            saving >= self.switch_minimum_usd
            and ratio >= self.switch_minimum_ratio
        )

    @staticmethod
    def evaluate_split(
        *,
        sequential_execution_ms: int,
        parallel_execution_ms: int,
        repeated_context_cost: float,
        handoff_cost: float,
        merge_cost: float,
        validation_cost: float,
        value_of_time_per_second: float,
    ) -> dict[str, Any]:
        time_saving = max(
            0, sequential_execution_ms - parallel_execution_ms
        ) / 1000
        benefit = time_saving * max(0.0, value_of_time_per_second)
        coordination = sum(
            max(0.0, value)
            for value in (
                repeated_context_cost,
                handoff_cost,
                merge_cost,
                validation_cost,
            )
        )
        return {
            "split": benefit > coordination,
            "parallel_saving_usd": round(benefit, 8),
            "coordination_cost_usd": round(coordination, 8),
            "net_value_usd": round(benefit - coordination, 8),
        }

    @staticmethod
    def regret(
        selected_cost: float,
        oracle_cost: float,
    ) -> float:
        if oracle_cost <= 0:
            return 0.0 if selected_cost <= 0 else math.inf
        return max(0.0, (selected_cost - oracle_cost) / oracle_cost)
