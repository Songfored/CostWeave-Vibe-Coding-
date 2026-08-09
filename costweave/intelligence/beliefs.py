"""Task, provider and system belief contracts for CostWeave v0.6.0."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .common import (
    DECISION_INTELLIGENCE_SCHEMA_VERSION,
    NonNegativeEstimate,
    ProbabilityInterval,
    RISK_LEVELS,
    bounded_text,
    canonical_json,
    finite_number,
    probability,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_sha256,
    validate_timestamp,
)


_FAMILY_KEY_RE = r"^[a-z][a-z0-9_.-]{1,63}$"


def _is_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative int")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


def _copy_json_object(value: Any, name: str, *, maximum_keys: int | None = None) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a JSON object")
    if maximum_keys is not None and len(value) > maximum_keys:
        raise ValueError(f"{name} has too many keys")
    try:
        normalized = canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain only finite JSON values") from exc
    result = __import__("json").loads(normalized)
    if not isinstance(result, dict):
        raise TypeError(f"{name} must be a JSON object")
    return result


def _family_probabilities(value: Any) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise TypeError("task_family_probabilities must be a mapping")
    result: dict[str, float] = {}
    for key, raw_probability in value.items():
        if not isinstance(key, str):
            raise TypeError("task family keys must be str")
        import re

        if re.fullmatch(_FAMILY_KEY_RE, key) is None:
            raise ValueError(f"invalid task family key: {key!r}")
        if key in result:
            raise ValueError(f"duplicate task family key: {key!r}")
        result[key] = probability(raw_probability, f"task_family_probabilities[{key}]")
    if not result or abs(sum(result.values()) - 1.0) > 1e-9:
        raise ValueError("task family probabilities must sum to 1 within 1e-9")
    return dict(sorted(result.items()))


@dataclass(frozen=True, slots=True)
class TaskBeliefV1:
    case_id: str
    objective_hash: str
    task_family_probabilities: Mapping[str, float]
    required_modalities: tuple[str, ...]
    required_tools: tuple[str, ...]
    risk_level: str
    reversible: bool
    external_side_effects: bool
    decomposability: ProbabilityInterval
    parallelizability: ProbabilityInterval
    verification_difficulty: ProbabilityInterval
    epistemic_uncertainty: float
    aleatoric_uncertainty: float
    ood_score: float
    hard_constraints: Mapping[str, Any]
    evidence_ids: tuple[str, ...]
    observed_at: str
    schema_version: str = DECISION_INTELLIGENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        object.__setattr__(self, "objective_hash", validate_sha256(self.objective_hash, "objective_hash"))
        object.__setattr__(self, "task_family_probabilities", _family_probabilities(self.task_family_probabilities))
        object.__setattr__(self, "required_modalities", unique_texts(self.required_modalities, "required_modalities", maximum_items=64))
        object.__setattr__(self, "required_tools", unique_texts(self.required_tools, "required_tools", maximum_items=64))
        if self.risk_level not in RISK_LEVELS:
            raise ValueError(f"unsupported risk_level: {self.risk_level!r}")
        object.__setattr__(self, "reversible", _is_bool(self.reversible, "reversible"))
        object.__setattr__(self, "external_side_effects", _is_bool(self.external_side_effects, "external_side_effects"))
        for field_name in ("decomposability", "parallelizability", "verification_difficulty"):
            if not isinstance(getattr(self, field_name), ProbabilityInterval):
                raise TypeError(f"{field_name} must be ProbabilityInterval")
        object.__setattr__(self, "epistemic_uncertainty", probability(self.epistemic_uncertainty, "epistemic_uncertainty"))
        object.__setattr__(self, "aleatoric_uncertainty", probability(self.aleatoric_uncertainty, "aleatoric_uncertainty"))
        object.__setattr__(self, "ood_score", probability(self.ood_score, "ood_score"))
        object.__setattr__(self, "hard_constraints", _copy_json_object(self.hard_constraints, "hard_constraints", maximum_keys=64))
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=64))
        object.__setattr__(self, "observed_at", validate_timestamp(self.observed_at, "observed_at"))
        if self.schema_version != DECISION_INTELLIGENCE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {DECISION_INTELLIGENCE_SCHEMA_VERSION}")

    def _identity(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "objective_hash": self.objective_hash,
            "task_family_probabilities": dict(sorted(self.task_family_probabilities.items())),
            "required_modalities": list(self.required_modalities),
            "required_tools": list(self.required_tools),
            "risk_level": self.risk_level,
            "reversible": self.reversible,
            "external_side_effects": self.external_side_effects,
            "decomposability": self.decomposability.to_dict(),
            "parallelizability": self.parallelizability.to_dict(),
            "verification_difficulty": self.verification_difficulty.to_dict(),
            "epistemic_uncertainty": self.epistemic_uncertainty,
            "aleatoric_uncertainty": self.aleatoric_uncertainty,
            "ood_score": self.ood_score,
            "hard_constraints": dict(sorted(self.hard_constraints.items())),
            "evidence_ids": list(self.evidence_ids),
            "schema_version": self.schema_version,
        }

    @property
    def belief_id(self) -> str:
        return stable_id("task_belief", self._identity())

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["observed_at"] = self.observed_at
        result["belief_id"] = self.belief_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TaskBeliefV1":
        fields = frozenset(
            {
                "case_id", "objective_hash", "task_family_probabilities", "required_modalities",
                "required_tools", "risk_level", "reversible", "external_side_effects",
                "decomposability", "parallelizability", "verification_difficulty",
                "epistemic_uncertainty", "aleatoric_uncertainty", "ood_score", "hard_constraints",
                "evidence_ids", "observed_at", "schema_version",
            }
        )
        validate_exact_fields(value, required=fields, optional=frozenset({"belief_id"}))
        item = cls(
            case_id=value["case_id"],
            objective_hash=value["objective_hash"],
            task_family_probabilities=value["task_family_probabilities"],
            required_modalities=tuple(value["required_modalities"]),
            required_tools=tuple(value["required_tools"]),
            risk_level=value["risk_level"],
            reversible=value["reversible"],
            external_side_effects=value["external_side_effects"],
            decomposability=ProbabilityInterval.from_dict(value["decomposability"]),
            parallelizability=ProbabilityInterval.from_dict(value["parallelizability"]),
            verification_difficulty=ProbabilityInterval.from_dict(value["verification_difficulty"]),
            epistemic_uncertainty=value["epistemic_uncertainty"],
            aleatoric_uncertainty=value["aleatoric_uncertainty"],
            ood_score=value["ood_score"],
            hard_constraints=value["hard_constraints"],
            evidence_ids=tuple(value["evidence_ids"]),
            observed_at=value["observed_at"],
            schema_version=value["schema_version"],
        )
        if "belief_id" in value and value["belief_id"] != item.belief_id:
            raise ValueError("belief_id does not match content")
        return item


@dataclass(frozen=True, slots=True)
class ProviderBeliefV1:
    provider_id: str
    deployment_id: str
    available: bool
    queue_latency_ms: NonNegativeEstimate
    completion_latency_ms: NonNegativeEstimate
    telemetry_age_ms: int
    price_snapshot_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", bounded_text(self.provider_id, "provider_id"))
        object.__setattr__(self, "deployment_id", bounded_text(self.deployment_id, "deployment_id"))
        object.__setattr__(self, "available", _is_bool(self.available, "available"))
        if not isinstance(self.queue_latency_ms, NonNegativeEstimate):
            raise TypeError("queue_latency_ms must be NonNegativeEstimate")
        if not isinstance(self.completion_latency_ms, NonNegativeEstimate):
            raise TypeError("completion_latency_ms must be NonNegativeEstimate")
        object.__setattr__(self, "telemetry_age_ms", _non_negative_int(self.telemetry_age_ms, "telemetry_age_ms"))
        object.__setattr__(self, "price_snapshot_id", bounded_text(self.price_snapshot_id, "price_snapshot_id"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "deployment_id": self.deployment_id,
            "available": self.available,
            "queue_latency_ms": self.queue_latency_ms.to_dict(),
            "completion_latency_ms": self.completion_latency_ms.to_dict(),
            "telemetry_age_ms": self.telemetry_age_ms,
            "price_snapshot_id": self.price_snapshot_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderBeliefV1":
        validate_exact_fields(
            value,
            required=frozenset(
                {"provider_id", "deployment_id", "available", "queue_latency_ms", "completion_latency_ms", "telemetry_age_ms", "price_snapshot_id"}
            ),
        )
        return cls(
            provider_id=value["provider_id"],
            deployment_id=value["deployment_id"],
            available=value["available"],
            queue_latency_ms=NonNegativeEstimate.from_dict(value["queue_latency_ms"]),
            completion_latency_ms=NonNegativeEstimate.from_dict(value["completion_latency_ms"]),
            telemetry_age_ms=value["telemetry_age_ms"],
            price_snapshot_id=value["price_snapshot_id"],
        )


@dataclass(frozen=True, slots=True)
class SystemBeliefV1:
    case_id: str
    local_queue_depth: int
    local_available_slots: int
    local_gpu_memory_available_mb: int | None
    budget_remaining_usd: float
    deadline_remaining_ms: int | None
    provider_beliefs: tuple[ProviderBeliefV1, ...]
    telemetry_age_ms: int
    evidence_ids: tuple[str, ...]
    observed_at: str
    schema_version: str = DECISION_INTELLIGENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        object.__setattr__(self, "local_queue_depth", _non_negative_int(self.local_queue_depth, "local_queue_depth"))
        object.__setattr__(self, "local_available_slots", _non_negative_int(self.local_available_slots, "local_available_slots"))
        memory = self.local_gpu_memory_available_mb
        if memory is not None:
            memory = _non_negative_int(memory, "local_gpu_memory_available_mb")
            if memory == 0:
                raise ValueError("unknown GPU memory must be None, not 0")
        object.__setattr__(self, "local_gpu_memory_available_mb", memory)
        object.__setattr__(self, "budget_remaining_usd", finite_number(self.budget_remaining_usd, "budget_remaining_usd", minimum=0.0))
        deadline = self.deadline_remaining_ms
        if deadline is not None:
            deadline = _non_negative_int(deadline, "deadline_remaining_ms")
        object.__setattr__(self, "deadline_remaining_ms", deadline)
        if not isinstance(self.provider_beliefs, (tuple, list)):
            raise TypeError("provider_beliefs must be a tuple")
        if len(self.provider_beliefs) > 128:
            raise ValueError("provider_beliefs must contain at most 128 items")
        providers: list[ProviderBeliefV1] = []
        seen: set[tuple[str, str]] = set()
        for item in self.provider_beliefs:
            if not isinstance(item, ProviderBeliefV1):
                raise TypeError("provider_beliefs must contain ProviderBeliefV1")
            key = (item.provider_id, item.deployment_id)
            if key in seen:
                raise ValueError("provider_id/deployment_id combinations must be unique")
            seen.add(key)
            providers.append(item)
        providers.sort(key=lambda item: (item.provider_id, item.deployment_id))
        object.__setattr__(self, "provider_beliefs", tuple(providers))
        object.__setattr__(self, "telemetry_age_ms", _non_negative_int(self.telemetry_age_ms, "telemetry_age_ms"))
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=64))
        object.__setattr__(self, "observed_at", validate_timestamp(self.observed_at, "observed_at"))
        if self.schema_version != DECISION_INTELLIGENCE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {DECISION_INTELLIGENCE_SCHEMA_VERSION}")

    def _identity(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "local_queue_depth": self.local_queue_depth,
            "local_available_slots": self.local_available_slots,
            "local_gpu_memory_available_mb": self.local_gpu_memory_available_mb,
            "budget_remaining_usd": self.budget_remaining_usd,
            "deadline_remaining_ms": self.deadline_remaining_ms,
            "provider_beliefs": [item.to_dict() for item in self.provider_beliefs],
            "telemetry_age_ms": self.telemetry_age_ms,
            "evidence_ids": list(self.evidence_ids),
            "schema_version": self.schema_version,
        }

    @property
    def belief_id(self) -> str:
        return stable_id("system_belief", self._identity())

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["observed_at"] = self.observed_at
        result["belief_id"] = self.belief_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SystemBeliefV1":
        fields = frozenset(
            {
                "case_id", "local_queue_depth", "local_available_slots", "local_gpu_memory_available_mb",
                "budget_remaining_usd", "deadline_remaining_ms", "provider_beliefs", "telemetry_age_ms",
                "evidence_ids", "observed_at", "schema_version",
            }
        )
        validate_exact_fields(value, required=fields, optional=frozenset({"belief_id"}))
        item = cls(
            case_id=value["case_id"],
            local_queue_depth=value["local_queue_depth"],
            local_available_slots=value["local_available_slots"],
            local_gpu_memory_available_mb=value["local_gpu_memory_available_mb"],
            budget_remaining_usd=value["budget_remaining_usd"],
            deadline_remaining_ms=value["deadline_remaining_ms"],
            provider_beliefs=tuple(ProviderBeliefV1.from_dict(provider) for provider in value["provider_beliefs"]),
            telemetry_age_ms=value["telemetry_age_ms"],
            evidence_ids=tuple(value["evidence_ids"]),
            observed_at=value["observed_at"],
            schema_version=value["schema_version"],
        )
        if "belief_id" in value and value["belief_id"] != item.belief_id:
            raise ValueError("belief_id does not match content")
        return item


__all__ = ["ProviderBeliefV1", "SystemBeliefV1", "TaskBeliefV1"]
