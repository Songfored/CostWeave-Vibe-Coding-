"""Versioned inference profiles and safe cache/batch policy interfaces."""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .domain import TaskContract, WorkerProfile


INFERENCE_POLICY_REVISION = "inference-policy-v1"


@dataclass(frozen=True, slots=True)
class InferenceProfile:
    revision: str
    reasoning_effort: str
    quantization: str
    batch_class: str
    cache_policy: str
    token_multiplier: float
    latency_multiplier: float
    compute_multiplier: float
    quality_delta: float
    cold_start_ms: int
    cache_hit_probability: float

    def to_dict(self) -> dict:
        return asdict(self)


class InferencePolicy:
    """Generate a small, deterministic profile frontier per worker."""

    @staticmethod
    def candidates(
        worker: WorkerProfile,
        task: TaskContract,
    ) -> tuple[InferenceProfile, ...]:
        cache_probability = (
            0.0
            if task.requires_freshness
            or task.risk_level == "high"
            or task.task_type in {"validation", "risk", "safety"}
            else 0.12
        )
        efficient = InferenceProfile(
            revision=INFERENCE_POLICY_REVISION + ":efficient",
            reasoning_effort="low",
            quantization="int8" if worker.local else "provider-default",
            batch_class="interactive",
            cache_policy=(
                "reuse-safe-prefix"
                if cache_probability
                else "no-reuse"
            ),
            token_multiplier=0.84,
            latency_multiplier=0.82,
            compute_multiplier=0.76,
            quality_delta=-0.016,
            cold_start_ms=260 if worker.local else 35,
            cache_hit_probability=cache_probability,
        )
        standard = InferenceProfile(
            revision=INFERENCE_POLICY_REVISION + ":standard",
            reasoning_effort="medium",
            quantization="fp16" if worker.local else "provider-default",
            batch_class="interactive",
            cache_policy="no-reuse",
            token_multiplier=1.0,
            latency_multiplier=1.0,
            compute_multiplier=1.0,
            quality_delta=0.0,
            cold_start_ms=420 if worker.local else 45,
            cache_hit_probability=0.0,
        )
        quality_gain = min(0.032, 0.012 + (1 - worker.reasoning) * 0.035)
        deep = InferenceProfile(
            revision=INFERENCE_POLICY_REVISION + ":deep",
            reasoning_effort="high",
            quantization="fp16" if worker.local else "provider-default",
            batch_class="interactive",
            cache_policy="no-reuse",
            token_multiplier=1.34,
            latency_multiplier=1.38,
            compute_multiplier=1.32,
            quality_delta=quality_gain,
            cold_start_ms=520 if worker.local else 55,
            cache_hit_probability=0.0,
        )
        return (efficient, standard, deep)

    @staticmethod
    def cache_key_scope(
        *,
        model_revision: str,
        prompt_revision: str,
        toolchain_revision: str,
        permission_scope: str,
        input_artifact_hash: str,
        freshness_epoch: str,
    ) -> tuple[str, ...]:
        """All fields are required before a cache entry can be reused."""

        values = (
            model_revision,
            prompt_revision,
            toolchain_revision,
            permission_scope,
            input_artifact_hash,
            freshness_epoch,
        )
        if any(not str(value).strip() for value in values):
            raise ValueError("cache scope fields cannot be empty")
        return tuple(str(value) for value in values)
