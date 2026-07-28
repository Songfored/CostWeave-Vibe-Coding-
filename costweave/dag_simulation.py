"""Seeded DAG Monte Carlo with recovery and common-failure factors."""

from __future__ import annotations

import hashlib
import math
import random
from collections import Counter
from typing import Any

from .outcome_forecast import (
    FORECAST_POLICY_REVISION,
    OUTCOME_CATEGORIES,
    OutcomeCategory,
    OutcomeForecast,
)


SIMULATION_REVISION = "dag-monte-carlo-v1"


def stable_seed(run_id: str, policy_revision: str = FORECAST_POLICY_REVISION) -> int:
    raw = hashlib.sha256(f"{run_id}:{policy_revision}".encode("utf-8")).digest()
    return int.from_bytes(raw[:8], "big") & ((1 << 63) - 1)


def _quantile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _critical_path_seconds(tasks: list[Any]) -> float:
    by_id = {task.id: task for task in tasks}
    memo: dict[str, float] = {}

    def duration(task_id: str) -> float:
        if task_id in memo:
            return memo[task_id]
        task = by_id[task_id]
        own = max(0.001, float(task.estimated_latency_ms) / 1000)
        parents = [item for item in task.dependencies if item in by_id]
        memo[task_id] = own + max((duration(item) for item in parents), default=0.0)
        return memo[task_id]

    return max((duration(task.id) for task in tasks), default=0.0)


class DagMonteCarlo:
    def simulate(
        self,
        *,
        run_id: str,
        plan: Any,
        request: Any,
        sample_size: int = 1200,
        seed: int | None = None,
        calibrated_probabilities: dict[str, float] | None = None,
    ) -> OutcomeForecast:
        if sample_size < 100:
            raise ValueError("sample_size must be at least 100")
        seed = stable_seed(run_id) if seed is None else int(seed)
        rng = random.Random(seed)
        tasks = list(plan.tasks)
        base_cost = sum(max(0.0, float(task.estimated_cost)) for task in tasks)
        base_time = _critical_path_seconds(tasks)
        counts = Counter({category: 0 for category in OUTCOME_CATEGORIES})
        costs: list[float] = []
        times: list[float] = []
        qualities: list[float] = []
        false_passes = 0
        failure_modes: Counter[str] = Counter()

        for _ in range(sample_size):
            common_factor = 0.86 + 0.14 * rng.betavariate(8, 2)
            failed = []
            false_pass = False
            for task in tasks:
                calibrated = (
                    calibrated_probabilities or {}
                ).get(task.id)
                prior = (
                    float(calibrated)
                    if calibrated is not None
                    else float(task.predicted_success)
                )
                task_probability = max(
                    0.02,
                    min(
                        0.995,
                        0.65 * prior
                        + 0.35 * float(
                            task.predicted_success_lower_bound
                            or task.predicted_success
                        ),
                    ),
                )
                if rng.random() > task_probability * common_factor:
                    failed.append(task)
                    validation_fp = float(
                        (task.validation or {}).get(
                            "false_positive_risk",
                            0.015 + 0.02 * float(task.uncertainty),
                        )
                    )
                    if task.criticality >= 0.7 and rng.random() < validation_fp:
                        false_pass = True

            if not failed:
                category = OutcomeCategory.FIRST_PASS.value
                multiplier, time_multiplier, quality_delta = 1.0, 1.0, 0.02
            elif false_pass:
                category = OutcomeCategory.FAILED.value
                multiplier, time_multiplier, quality_delta = 1.08, 1.05, -0.22
                false_passes += 1
                failure_modes["critical_false_pass"] += 1
            elif request.max_recoveries <= 0:
                category = OutcomeCategory.FAILED.value
                multiplier, time_multiplier, quality_delta = 1.02, 1.0, -0.18
                failure_modes["recovery_disabled"] += 1
            else:
                pressure = min(0.22, 0.025 * len(failed))
                draw = rng.random()
                repair_cut = max(0.20, 0.50 - pressure)
                upgrade_cut = repair_cut + 0.29
                human_cut = upgrade_cut + 0.11
                if draw < repair_cut:
                    category = OutcomeCategory.REPAIRED.value
                    multiplier, time_multiplier, quality_delta = 1.18, 1.28, -0.01
                elif draw < upgrade_cut:
                    category = OutcomeCategory.UPGRADED.value
                    multiplier, time_multiplier, quality_delta = 1.42, 1.42, 0.01
                elif draw < human_cut:
                    category = OutcomeCategory.HUMAN.value
                    multiplier, time_multiplier, quality_delta = 1.55, 1.8, 0.015
                else:
                    category = OutcomeCategory.FAILED.value
                    multiplier, time_multiplier, quality_delta = 1.16, 1.34, -0.20
                    failure_modes["recovery_exhausted"] += 1

            counts[category] += 1
            jitter = 0.88 + 0.24 * rng.random()
            costs.append(base_cost * multiplier * jitter)
            times.append(base_time * time_multiplier * (0.9 + 0.25 * rng.random()))
            base_quality = sum(
                float(
                    (calibrated_probabilities or {}).get(
                        task.id, task.predicted_success
                    )
                )
                for task in tasks
            ) / max(1, len(tasks))
            qualities.append(max(0.0, min(1.0, base_quality + quality_delta + rng.uniform(-0.025, 0.025))))

        probabilities = {
            category: counts[category] / sample_size
            for category in OUTCOME_CATEGORIES
        }
        first = probabilities[OutcomeCategory.FIRST_PASS.value]
        repaired = first + probabilities[OutcomeCategory.REPAIRED.value]
        upgraded = repaired + probabilities[OutcomeCategory.UPGRADED.value]
        human = upgraded + probabilities[OutcomeCategory.HUMAN.value]
        ood_reasons = []
        if plan.estimate_confidence < 0.65:
            ood_reasons.append("low_plan_confidence")
        if any(task.route_confidence < 0.55 for task in tasks):
            ood_reasons.append("low_route_confidence")
        if any(task.required_modalities != ["text"] for task in tasks):
            ood_reasons.append("non_text_modality")
        confidence = max(
            0.1,
            min(
                0.98,
                0.45 * float(plan.estimate_confidence)
                + 0.35 * (
                    sum(float(task.route_confidence) for task in tasks)
                    / max(1, len(tasks))
                )
                + 0.2 * min(1.0, sample_size / 1000),
            ),
        )
        modes = tuple(
            {"mode": name, "probability": count / sample_size}
            for name, count in failure_modes.most_common(5)
        )
        return OutcomeForecast.create(
            run_id=run_id,
            probabilities=probabilities,
            cumulative_success={
                "first_pass": first,
                "after_repair": repaired,
                "after_upgrade": upgraded,
                "after_human": human,
            },
            quality={
                "p50": round(_quantile(qualities, 0.5), 6),
                "p95": round(_quantile(qualities, 0.95), 6),
            },
            cost={
                "p50": round(_quantile(costs, 0.5), 8),
                "p95": round(_quantile(costs, 0.95), 8),
            },
            completion_seconds={
                "p50": round(_quantile(times, 0.5), 6),
                "p95": round(_quantile(times, 0.95), 6),
            },
            confidence=round(confidence, 6),
            sample_size=sample_size,
            seed=seed,
            out_of_distribution=bool(ood_reasons),
            ood_reasons=tuple(ood_reasons),
            major_failure_modes=modes,
            critical_false_pass_probability=round(false_passes / sample_size, 8),
        )
