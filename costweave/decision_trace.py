"""Auditable decision trace contract for CostWeave v0.6.0."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..security_boundary import normalize_project_id
from .common import (
    DECISION_INTELLIGENCE_SCHEMA_VERSION,
    META_ACTIONS,
    bounded_text,
    probability,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_timestamp,
)


@dataclass(frozen=True, slots=True)
class DecisionTraceV2:
    project_id: str
    case_id: str
    task_belief_id: str
    system_belief_id: str
    candidate_strategy_ids: tuple[str, ...]
    candidate_probabilities: Mapping[str, float]
    selected_strategy_id: str
    meta_action: str
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    policy_id: str
    policy_version: str
    created_at: str
    schema_version: str = DECISION_INTELLIGENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        object.__setattr__(self, "task_belief_id", bounded_text(self.task_belief_id, "task_belief_id"))
        object.__setattr__(self, "system_belief_id", bounded_text(self.system_belief_id, "system_belief_id"))
        candidates = unique_texts(self.candidate_strategy_ids, "candidate_strategy_ids", maximum_items=256)
        if not 1 <= len(candidates) <= 256:
            raise ValueError("candidate_strategy_ids must contain between 1 and 256 items")
        object.__setattr__(self, "candidate_strategy_ids", candidates)
        if not isinstance(self.candidate_probabilities, Mapping):
            raise TypeError("candidate_probabilities must be a mapping")
        probabilities: dict[str, float] = {}
        for key, value in self.candidate_probabilities.items():
            if not isinstance(key, str):
                raise TypeError("candidate probability keys must be str")
            probabilities[key] = probability(value, f"candidate_probabilities[{key}]")
        if set(probabilities) != set(candidates):
            raise ValueError("candidate probability keys must exactly match candidates")
        if abs(sum(probabilities.values()) - 1.0) > 1e-9:
            raise ValueError("candidate probabilities must sum to 1 within 1e-9")
        object.__setattr__(self, "candidate_probabilities", dict(sorted(probabilities.items())))
        object.__setattr__(self, "selected_strategy_id", bounded_text(self.selected_strategy_id, "selected_strategy_id"))
        if self.selected_strategy_id not in probabilities:
            raise ValueError("selected strategy must be one of the candidates")
        if probabilities[self.selected_strategy_id] <= 0.0:
            raise ValueError("selected strategy probability must be > 0")
        if self.meta_action not in META_ACTIONS:
            raise ValueError(f"unsupported meta_action: {self.meta_action!r}")
        reasons = unique_texts(self.reason_codes, "reason_codes", maximum_items=64)
        if not reasons:
            raise ValueError("reason_codes must contain at least one item")
        object.__setattr__(self, "reason_codes", reasons)
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=64))
        object.__setattr__(self, "policy_id", bounded_text(self.policy_id, "policy_id"))
        object.__setattr__(self, "policy_version", bounded_text(self.policy_version, "policy_version"))
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if self.schema_version != DECISION_INTELLIGENCE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {DECISION_INTELLIGENCE_SCHEMA_VERSION}")

    def _identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "case_id": self.case_id,
            "task_belief_id": self.task_belief_id,
            "system_belief_id": self.system_belief_id,
            "candidate_strategy_ids": list(self.candidate_strategy_ids),
            "candidate_probabilities": dict(sorted(self.candidate_probabilities.items())),
            "selected_strategy_id": self.selected_strategy_id,
            "meta_action": self.meta_action,
            "reason_codes": list(self.reason_codes),
            "evidence_ids": list(self.evidence_ids),
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "schema_version": self.schema_version,
        }

    @property
    def trace_id(self) -> str:
        return stable_id("decision_trace", self._identity())

    def to_dict(self) -> dict[str, Any]:
        result = self._identity()
        result["created_at"] = self.created_at
        result["trace_id"] = self.trace_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "DecisionTraceV2":
        fields = frozenset(
            {
                "project_id", "case_id", "task_belief_id", "system_belief_id", "candidate_strategy_ids",
                "candidate_probabilities", "selected_strategy_id", "meta_action", "reason_codes",
                "evidence_ids", "policy_id", "policy_version", "created_at", "schema_version",
            }
        )
        validate_exact_fields(value, required=fields, optional=frozenset({"trace_id"}))
        item = cls(
            project_id=value["project_id"],
            case_id=value["case_id"],
            task_belief_id=value["task_belief_id"],
            system_belief_id=value["system_belief_id"],
            candidate_strategy_ids=tuple(value["candidate_strategy_ids"]),
            candidate_probabilities=value["candidate_probabilities"],
            selected_strategy_id=value["selected_strategy_id"],
            meta_action=value["meta_action"],
            reason_codes=tuple(value["reason_codes"]),
            evidence_ids=tuple(value["evidence_ids"]),
            policy_id=value["policy_id"],
            policy_version=value["policy_version"],
            created_at=value["created_at"],
            schema_version=value["schema_version"],
        )
        if "trace_id" in value and value["trace_id"] != item.trace_id:
            raise ValueError("trace_id does not match content")
        return item


__all__ = ["DecisionTraceV2"]
