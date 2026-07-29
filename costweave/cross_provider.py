"""Provider-neutral deployment economics and pre-execution admission."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Callable, Iterable

from .calibration import BetaPosterior, HierarchicalCalibrator
from .domain import TaskContract, WorkerProfile
from .inference_policy import InferenceProfile
from .resource_inventory import ResourcePool


CROSS_PROVIDER_POLICY_REVISION = "cross-provider-economics-0.5.4"

PosteriorLookup = Callable[
    ["DeploymentKey", TaskContract],
    Iterable[tuple[str, BetaPosterior]],
]


@dataclass(frozen=True, slots=True)
class DeploymentKey:
    model_id: str
    provider: str
    endpoint: str
    reasoning_effort: str
    task_family: str

    @property
    def value(self) -> str:
        raw = "|".join(
            (
                self.model_id,
                self.provider,
                self.endpoint,
                self.reasoning_effort,
                self.task_family,
            )
        )
        return "deployment_" + hashlib.sha256(
            raw.encode("utf-8")
        ).hexdigest()[:32]

    def to_dict(self) -> dict[str, str]:
        return {**asdict(self), "key": self.value}


@dataclass(frozen=True, slots=True)
class DeploymentAssessment:
    deployment: DeploymentKey
    admitted: bool
    admission_code: str
    admission_reasons: tuple[str, ...]
    predicted_success: float
    success_lower_bound: float
    uncertainty: float
    posterior_level: str
    posterior_samples: float
    cold_start: bool
    catalog_freshness: float
    execution_p50_ms: int
    finish_p95_ms: int
    deadline_miss_probability: float
    capacity: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "deployment": self.deployment.to_dict(),
            "admitted": self.admitted,
            "admission_code": self.admission_code,
            "admission_reasons": list(self.admission_reasons),
            "predicted_success": round(self.predicted_success, 6),
            "success_lower_bound": round(
                self.success_lower_bound, 6
            ),
            "uncertainty": round(self.uncertainty, 6),
            "posterior_level": self.posterior_level,
            "posterior_samples": round(self.posterior_samples, 3),
            "cold_start": self.cold_start,
            "catalog_freshness": round(self.catalog_freshness, 4),
            "execution_p50_ms": self.execution_p50_ms,
            "finish_p95_ms": self.finish_p95_ms,
            "deadline_miss_probability": round(
                self.deadline_miss_probability, 6
            ),
            "capacity": dict(self.capacity),
            "policy_revision": CROSS_PROVIDER_POLICY_REVISION,
        }


class CrossProviderPolicy:
    """Calibrate a deployment and reject predictable waste before execution."""

    def __init__(
        self,
        posterior_lookup: PosteriorLookup | None = None,
        *,
        minimum_samples: int = 12,
    ) -> None:
        self.posterior_lookup = posterior_lookup
        self.calibrator = HierarchicalCalibrator(
            minimum_samples=minimum_samples
        )

    @staticmethod
    def deployment_key(
        worker: WorkerProfile,
        profile: InferenceProfile,
        task: TaskContract,
    ) -> DeploymentKey:
        model_id = worker.model_id or worker.id
        endpoint = (
            f"local://{worker.id}"
            if worker.local
            else f"{worker.provider}://{model_id}"
        )
        return DeploymentKey(
            model_id=model_id,
            provider=worker.provider,
            endpoint=endpoint,
            reasoning_effort=profile.reasoning_effort,
            task_family=task.task_type,
        )

    @staticmethod
    def catalog_freshness(worker: WorkerProfile) -> float:
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

    @staticmethod
    def _deadline_miss_probability(
        finish_p95_ms: int,
        deadline_ms: int | None,
        uncertainty: float,
    ) -> float:
        if deadline_ms is None:
            return 0.0
        ratio = finish_p95_ms / max(1, deadline_ms)
        # A smooth forecast makes near-deadline choices visibly risky before
        # the hard p95 gate rejects them.
        logistic = 1.0 / (1.0 + math.exp(-8.0 * (ratio - 0.82)))
        return max(
            0.0,
            min(1.0, logistic + max(0.0, uncertainty) * 0.35),
        )

    def assess(
        self,
        *,
        task: TaskContract,
        worker: WorkerProfile,
        pool: ResourcePool,
        profile: InferenceProfile,
        prior_mean: float,
        prior_lower: float,
        prior_uncertainty: float,
        base_latency_ms: int,
        availability: dict[str, Any] | None,
        deadline_ms: int | None,
    ) -> DeploymentAssessment:
        available = dict(availability or {})
        key = self.deployment_key(worker, profile, task)
        posterior_candidates = (
            list(self.posterior_lookup(key, task))
            if self.posterior_lookup is not None
            else []
        )
        calibrated = self.calibrator.calibrate(
            prior_mean, posterior_candidates
        )
        samples = float(calibrated["samples"])
        cold_start = samples < 4
        freshness = self.catalog_freshness(worker)
        # Sparse or stale evidence should reduce confidence without forcing
        # every cold-start deployment onto an unnecessarily expensive tier.
        stale_penalty = (1.0 - freshness) * 0.008
        cold_penalty = 0.004 if cold_start else 0.0
        calibrated_mean = max(
            0.03,
            min(
                0.997,
                float(calibrated["probability"])
                - stale_penalty
                - cold_penalty,
            ),
        )
        delta = calibrated_mean - prior_mean
        uncertainty = max(
            0.012,
            min(
                0.35,
                prior_uncertainty
                + (0.018 if cold_start else 0.0)
                + (1.0 - freshness) * 0.025
                - min(0.015, samples / 2000.0),
            ),
        )
        lower = max(
            0.03,
            min(
                calibrated_mean,
                prior_lower + delta * 0.72
                - (0.002 if cold_start else 0.0)
                - stale_penalty * 0.25,
            ),
        )

        execution_ms = max(
            1,
            round(
                base_latency_ms * profile.latency_multiplier
                + profile.cold_start_ms
            ),
        )
        slots = int(available.get("slots", pool.slots_total))
        queue_depth = max(0, int(available.get("queue_depth", 0)))
        queue_ms = round(
            queue_depth / max(1, pool.slots_total) * execution_ms
        )
        finish_p95 = queue_ms + round(
            execution_ms * (1.32 + uncertainty * 0.7)
        )
        deadline_miss = self._deadline_miss_probability(
            finish_p95, deadline_ms, uncertainty
        )

        reasons: list[str] = []
        state = str(
            available.get("state", pool.state)
        ).strip().lower()
        circuit_state = str(
            available.get("circuit_state", "closed")
        ).strip().lower()
        provider_health = float(
            available.get("provider_health", 1.0)
        )
        bulkhead_limit = max(
            1, int(available.get("bulkhead_limit", pool.slots_total))
        )
        bulkhead_active = max(
            0,
            int(
                available.get(
                    "bulkhead_active",
                    max(0, pool.slots_total - slots),
                )
            ),
        )
        available_vram = int(
            available.get("vram_mb", pool.vram_mb_total)
        )
        if state not in {"available", "healthy", "ready"}:
            reasons.append("resource_pool_unavailable")
        if circuit_state in {"open", "half-open"}:
            reasons.append("provider_circuit_open")
        if provider_health < 0.35:
            reasons.append("provider_health_below_floor")
        if bulkhead_active >= bulkhead_limit:
            reasons.append("provider_bulkhead_saturated")
        if queue_depth >= pool.queue_limit:
            reasons.append("queue_limit_reached")
        if worker.local and slots <= 0:
            reasons.append("local_capacity_saturated")
        if (
            worker.local
            and task.vram_mb > 0
            and task.vram_mb > available_vram
        ):
            reasons.append("local_vram_unavailable")
        if deadline_ms is not None and finish_p95 > deadline_ms:
            reasons.append("deadline_p95_miss")

        admitted = not reasons
        code = "admitted"
        if reasons:
            code = (
                "diverted"
                if any(
                    reason.startswith("local_")
                    or reason == "deadline_p95_miss"
                    for reason in reasons
                )
                else "rejected"
            )
        capacity = {
            "pool_id": pool.pool_id,
            "local": worker.local,
            "slots_available": slots,
            "slots_total": pool.slots_total,
            "queue_depth": queue_depth,
            "queue_limit": pool.queue_limit,
            "vram_available_mb": available_vram,
            "vram_total_mb": pool.vram_mb_total,
            "bulkhead_active": bulkhead_active,
            "bulkhead_limit": bulkhead_limit,
            "circuit_state": circuit_state,
            "provider_health": round(provider_health, 4),
        }
        return DeploymentAssessment(
            deployment=key,
            admitted=admitted,
            admission_code=code,
            admission_reasons=tuple(reasons),
            predicted_success=calibrated_mean,
            success_lower_bound=lower,
            uncertainty=uncertainty,
            posterior_level=str(calibrated["level"]),
            posterior_samples=samples,
            cold_start=cold_start,
            catalog_freshness=freshness,
            execution_p50_ms=execution_ms,
            finish_p95_ms=finish_p95,
            deadline_miss_probability=deadline_miss,
            capacity=capacity,
        )
