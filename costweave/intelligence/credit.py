"""Outcome credit and learning-safety contract for CostWeave v0.6.0."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..security_boundary import normalize_project_id
from .common import (
    CREDIT_COMPONENTS,
    DECISION_INTELLIGENCE_SCHEMA_VERSION,
    EVIDENCE_LEVELS,
    bounded_text,
    finite_number,
    probability,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_timestamp,
)


CREDIT_METHODS = (
    "deterministic",
    "counterfactual_replay",
    "hierarchical_estimate",
    "ips_dr",
    "llm_attribution",
)


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative int")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


@dataclass(frozen=True, slots=True)
class OutcomeCreditV1:
    project_id: str
    trace_id: str
    accepted: bool
    total_cost_usd: float
    total_latency_ms: int
    component_weights: Mapping[str, float]
    method: str
    evidence_level: str
    usable_for_learning: bool
    evidence_ids: tuple[str, ...]
    created_at: str
    schema_version: str = DECISION_INTELLIGENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "trace_id", bounded_text(self.trace_id, "trace_id"))
        if not isinstance(self.accepted, bool):
            raise TypeError("accepted must be bool")
        object.__setattr__(self, "total_cost_usd", finite_number(self.total_cost_usd, "total_cost_usd", minimum=0.0))
        object.__setattr__(self, "total_latency_ms", _non_negative_int(self.total_latency_ms, "total_latency_ms"))
        if not isinstance(self.component_weights, Mapping):
            raise TypeError("component_weights must be a mapping")
        if set(self.component_weights) != set(CREDIT_COMPONENTS):
            raise ValueError("component_weights keys must exactly match CREDIT_COMPONENTS")
        weights: dict[str, float] = {}
        for key, value in self.component_weights.items():
            if not isinstance(key, str):
                raise TypeError("component weight keys must be str")
            weights[key] = probability(value, f"component_weights[{key}]")
        if abs(sum(weights.values()) - 1.0) > 1e-9:
            raise ValueError("component weights must sum to 1 within 1e-9")
        object.__setattr__(self, "component_weights", dict(sorted(weights.items())))
        if self.method not in CREDIT_METHODS:
            raise ValueError(f"unsupported credit method: {self.method!r}")
        if self.evidence_level not in EVIDENCE_LEVELS:
            raise ValueError(f"unsupported evidence_level: {self.evidence_level!r}")
        if not isinstance(self.usable_for_learning, bool):
            raise TypeError("usable_for_learning must be bool")
        if self.method == "llm_attribution" and self.usable_for_learning:
            raise ValueError("llm_attribution credit cannot be used for learning")
        if self.evidence_level in ("E0-simulated", "E1-public-replay") and self.usable_for_learning:
            raise ValueError("E0/E1 credit cannot be used for learning")
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=64))
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if self.schema_version != DECISION_INTELLIGENCE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {DECISION_INTELLIGENCE_SCHEMA_VERSION}")

    def _identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "trace_id": self.trace_id,
            "accepted": self.accepted,
            "total_cost_usd": self.total_cost_usd,
            "total_latency_ms": self.total_latency_ms,
            "component_weights": dict(sorted(self.component_weights.items())),
            "method": self.method,
            "evidence_level": self.evidence_level,
            "usable_for_learning": self.usable_for_learning,
            "evidence_ids": list(self.evidence_ids),
            "schema_version": self.schema_version,
        }

    @property
    def credit_id(self) -> str:
        return stable_id("outcome_credit", self._identity())

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["created_at"] = self.created_at
        result["credit_id"] = self.credit_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "OutcomeCreditV1":
        fields = frozenset(
            {
                "project_id", "trace_id", "accepted", "total_cost_usd", "total_latency_ms",
                "component_weights", "method", "evidence_level", "usable_for_learning",
                "evidence_ids", "created_at", "schema_version",
            }
        )
        validate_exact_fields(value, required=fields, optional=frozenset({"credit_id"}))
        item = cls(
            project_id=value["project_id"],
            trace_id=value["trace_id"],
            accepted=value["accepted"],
            total_cost_usd=value["total_cost_usd"],
            total_latency_ms=value["total_latency_ms"],
            component_weights=value["component_weights"],
            method=value["method"],
            evidence_level=value["evidence_level"],
            usable_for_learning=value["usable_for_learning"],
            evidence_ids=tuple(value["evidence_ids"]),
            created_at=value["created_at"],
            schema_version=value["schema_version"],
        )
        if "credit_id" in value and value["credit_id"] != item.credit_id:
            raise ValueError("credit_id does not match content")
        return item


__all__ = ["CREDIT_METHODS", "OutcomeCreditV1"]
