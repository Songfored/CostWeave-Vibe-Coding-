"""Local compute, energy, shadow-price and failure-loss economics."""

from __future__ import annotations

from dataclasses import dataclass

from .cost_model import CostBreakdown, CostForecast
from .domain import TaskContract, WorkerProfile
from .inference_policy import InferenceProfile
from .resource_inventory import ResourcePool


@dataclass(frozen=True, slots=True)
class EconomicsConfig:
    cpu_second_price: float = 0.000004
    gpu_second_price: float = 0.00016
    memory_gb_second_price: float = 0.0000005
    energy_price_per_kwh: float = 0.14
    cpu_power_kw: float = 0.065
    gpu_power_kw: float = 0.35
    depreciation_per_second: float = 0.000018
    queue_opportunity_per_second: float = 0.000003
    handoff_per_1k_tokens: float = 0.000012
    validation_base: float = 0.000018
    detection_base: float = 0.00004
    rework_multiplier: float = 1.18
    invalidation_multiplier: float = 0.55


class ResourceEconomics:
    def __init__(self, config: EconomicsConfig | None = None) -> None:
        self.config = config or EconomicsConfig()

    def forecast(
        self,
        *,
        task: TaskContract,
        worker: WorkerProfile,
        pool: ResourcePool,
        profile: InferenceProfile,
        direct_cost: float,
        success_lower_bound: float,
        execution_ms: int,
        queue_ms: int,
        available_slots: int,
        value_of_time_per_second: float | None = None,
    ) -> CostForecast:
        seconds = max(0.001, execution_ms / 1000)
        cache_discount = (
            1 - profile.cache_hit_probability * 0.55
        )
        provider_cost = (
            max(0.0, direct_cost)
            * profile.token_multiplier
            * cache_discount
            if not worker.local
            else 0.0
        )

        local_compute = 0.0
        energy = 0.0
        if worker.local:
            cpu_cost = (
                seconds
                * max(0.0, task.cpu_cores)
                * self.config.cpu_second_price
            )
            gpu_cost = (
                seconds
                * max(0, task.gpu_count)
                * self.config.gpu_second_price
            )
            memory_cost = (
                seconds
                * max(0, task.vram_mb)
                / 1024
                * self.config.memory_gb_second_price
            )
            local_compute = (
                cpu_cost + gpu_cost + memory_cost
            ) * profile.compute_multiplier
            local_compute += (
                seconds
                * self.config.depreciation_per_second
                * profile.compute_multiplier
            )
            power_kw = (
                max(0.0, task.cpu_cores) * self.config.cpu_power_kw
                + max(0, task.gpu_count) * self.config.gpu_power_kw
            )
            energy = (
                power_kw
                * seconds
                / 3600
                * self.config.energy_price_per_kwh
                * profile.compute_multiplier
            )

        utilization = 1 - (
            max(0, available_slots)
            / max(1, pool.slots_total)
        )
        scarce_capacity = (
            seconds
            * utilization**2
            * (self.config.gpu_second_price if task.gpu_count else 0.00002)
            * max(0.25, task.criticality)
        )
        time_price = (
            self.config.queue_opportunity_per_second
            if value_of_time_per_second is None
            else max(0.0, value_of_time_per_second)
        )
        queue_opportunity = (
            max(0, queue_ms) / 1000
            * time_price
            * max(0.25, task.criticality)
        )
        handoff = (
            (task.estimated_input_tokens + task.estimated_output_tokens)
            / 1000
            * self.config.handoff_per_1k_tokens
            * (0.55 if profile.cache_hit_probability else 1.0)
        )
        validation_merge = self.config.validation_base * (
            1
            + len(task.dependencies) * 0.25
            + (0.7 if task.task_type == "validation" else 0)
        )
        base_before_failure = (
            provider_cost
            + local_compute
            + energy
            + scarce_capacity
            + queue_opportunity
            + handoff
            + validation_merge
        )
        failure_probability = max(
            0.0, min(0.97, 1 - success_lower_bound)
        )
        failure_loss = (
            self.config.detection_base
            + base_before_failure * self.config.rework_multiplier
            + base_before_failure
            * self.config.invalidation_multiplier
            * max(0.2, task.criticality)
        )
        expected_rework = failure_probability * failure_loss
        breakdown = CostBreakdown(
            provider_inference=provider_cost,
            local_compute=local_compute,
            energy=energy,
            scarce_capacity=scarce_capacity,
            queue_opportunity=queue_opportunity,
            handoff=handoff,
            validation_merge=validation_merge,
            expected_rework=expected_rework,
        )
        p95 = breakdown.total * (
            1.16 + failure_probability * 0.34
        )
        ceiling = p95 * 1.08
        return CostForecast(
            p50=breakdown,
            p95_total=p95,
            reservation_ceiling=ceiling,
        )
