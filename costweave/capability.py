"""Evidence-backed model capability posterior contracts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping

from .common import (
    DECISION_INTELLIGENCE_SCHEMA_VERSION,
    EVIDENCE_LEVELS,
    NonNegativeEstimate,
    ProbabilityInterval,
    bounded_text,
    probability,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_timestamp,
)


_TASK_FAMILY_RE = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative int")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


@dataclass(frozen=True, slots=True)
class CapabilityPosteriorV1:
    candidate_id: str
    task_family: str
    model_revision: str
    reasoning_tier: str
    provider_deployment: str
    success: ProbabilityInterval
    cost_usd: NonNegativeEstimate
    latency_ms: NonNegativeEstimate
    retry_probability: float
    escalation_probability: float
    rework_probability: float
    evidence_count: int
    evidence_level: str
    correlation_group: str
    source_ids: tuple[str, ...]
    as_of: str
    schema_version: str = DECISION_INTELLIGENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "candidate_id", bounded_text(self.candidate_id, "candidate_id"))
        object.__setattr__(self, "task_family", bounded_text(self.task_family, "task_family"))
        if _TASK_FAMILY_RE.fullmatch(self.task_family) is None:
            raise ValueError("task_family has an invalid format")
        object.__setattr__(self, "model_revision", bounded_text(self.model_revision, "model_revision"))
        object.__setattr__(self, "reasoning_tier", bounded_text(self.reasoning_tier, "reasoning_tier"))
        object.__setattr__(self, "provider_deployment", bounded_text(self.provider_deployment, "provider_deployment"))
        if not isinstance(self.success, ProbabilityInterval):
            raise TypeError("success must be ProbabilityInterval")
        if not isinstance(self.cost_usd, NonNegativeEstimate):
            raise TypeError("cost_usd must be NonNegativeEstimate")
        if not isinstance(self.latency_ms, NonNegativeEstimate):
            raise TypeError("latency_ms must be NonNegativeEstimate")
        object.__setattr__(self, "retry_probability", probability(self.retry_probability, "retry_probability"))
        object.__setattr__(self, "escalation_probability", probability(self.escalation_probability, "escalation_probability"))
        object.__setattr__(self, "rework_probability", probability(self.rework_probability, "rework_probability"))
        object.__setattr__(self, "evidence_count", _non_negative_int(self.evidence_count, "evidence_count"))
        if self.evidence_level not in EVIDENCE_LEVELS:
            raise ValueError(f"unsupported evidence_level: {self.evidence_level!r}")
        if EVIDENCE_LEVELS.index(self.evidence_level) >= EVIDENCE_LEVELS.index("E2-directional-real") and self.evidence_count <= 0:
            raise ValueError("E2 and higher evidence require evidence_count > 0")
        object.__setattr__(self, "correlation_group", bounded_text(self.correlation_group, "correlation_group"))
        object.__setattr__(self, "source_ids", unique_texts(self.source_ids, "source_ids", maximum_items=64))
        object.__setattr__(self, "as_of", validate_timestamp(self.as_of, "as_of"))
        if self.schema_version != DECISION_INTELLIGENCE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {DECISION_INTELLIGENCE_SCHEMA_VERSION}")

    def _identity(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "task_family": self.task_family,
            "model_revision": self.model_revision,
            "reasoning_tier": self.reasoning_tier,
            "provider_deployment": self.provider_deployment,
            "success": self.success.to_dict(),
            "cost_usd": self.cost_usd.to_dict(),
            "latency_ms": self.latency_ms.to_dict(),
            "retry_probability": self.retry_probability,
            "escalation_probability": self.escalation_probability,
            "rework_probability": self.rework_probability,
            "evidence_count": self.evidence_count,
            "evidence_level": self.evidence_level,
            "correlation_group": self.correlation_group,
            "source_ids": list(self.source_ids),
            "schema_version": self.schema_version,
        }

    @property
    def posterior_id(self) -> str:
        return stable_id("capability_posterior", self._identity())

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["as_of"] = self.as_of
        result["posterior_id"] = self.posterior_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityPosteriorV1":
        fields = frozenset(
            {
                "candidate_id", "task_family", "model_revision", "reasoning_tier", "provider_deployment",
                "success", "cost_usd", "latency_ms", "retry_probability", "escalation_probability",
                "rework_probability", "evidence_count", "evidence_level", "correlation_group",
                "source_ids", "as_of", "schema_version",
            }
        )
        validate_exact_fields(value, required=fields, optional=frozenset({"posterior_id"}))
        item = cls(
            candidate_id=value["candidate_id"],
            task_family=value["task_family"],
            model_revision=value["model_revision"],
            reasoning_tier=value["reasoning_tier"],
            provider_deployment=value["provider_deployment"],
            success=ProbabilityInterval.from_dict(value["success"]),
            cost_usd=NonNegativeEstimate.from_dict(value["cost_usd"]),
            latency_ms=NonNegativeEstimate.from_dict(value["latency_ms"]),
            retry_probability=value["retry_probability"],
            escalation_probability=value["escalation_probability"],
            rework_probability=value["rework_probability"],
            evidence_count=value["evidence_count"],
            evidence_level=value["evidence_level"],
            correlation_group=value["correlation_group"],
            source_ids=tuple(value["source_ids"]),
            as_of=value["as_of"],
            schema_version=value["schema_version"],
        )
        if "posterior_id" in value and value["posterior_id"] != item.posterior_id:
            raise ValueError("posterior_id does not match content")
        return item


__all__ = ["CapabilityPosteriorV1"]
