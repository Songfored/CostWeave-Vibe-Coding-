"""Trace credit assignment and safe offline learning contracts for v0.6.5.

This module is deliberately advisory-only.  It converts an independently
validated execution trace into layered credit, evaluates a candidate policy
from logged observations, and records promotion gates without mutating a live
route.  The E0 demo is a fixture for exercising the contracts, never a
training result.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ..security_boundary import normalize_project_id
from .common import (
    CREDIT_COMPONENTS,
    EVIDENCE_LEVELS,
    RISK_LEVELS,
    bounded_text,
    canonical_json,
    finite_number,
    probability,
    stable_digest,
    validate_exact_fields,
    validate_timestamp,
    unique_texts,
)


SAFE_LEARNING_SCHEMA_VERSION = "v0.6.5"
SAFE_LEARNING_POLICY_REVISION = "trace-credit-assignment-safe-learning-v1"
SAFE_LEARNING_CONFIRM_TOKEN = "RUN_SAFE_LEARNING"

CREDIT_METHODS = (
    "deterministic",
    "counterfactual_replay",
    "hierarchical_estimate",
    "ips",
    "dr",
)
LEARNING_STAGES = ("offline", "shadow", "canary")
LEARNING_STATES = ("candidate", "shadow_eligible", "frozen", "rejected", "rollback")
REAL_DATA_ORIGINS = frozenset({"production", "human_benchmark"})
ALL_DATA_ORIGINS = REAL_DATA_ORIGINS | frozenset({"test_fixture", "simulated"})
MIN_LEARNING_SAMPLES = 8
MAX_IMPORTANCE_WEIGHT = 20.0

_EVIDENCE_RANK = {name: index for index, name in enumerate(EVIDENCE_LEVELS)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, name: str, *, maximum: int = 200) -> str:
    return bounded_text(value, name, maximum=maximum)


def _finite(value: Any, name: str, *, minimum: float, maximum: float) -> float:
    result = finite_number(value, name, minimum=minimum)
    if result > maximum:
        raise ValueError(f"{name} must be <= {maximum}")
    return result


def _components(value: Mapping[str, Any], name: str, *, minimum: float, maximum: float) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be a mapping")
    if set(value) != set(CREDIT_COMPONENTS):
        raise ValueError(f"{name} keys must exactly match CREDIT_COMPONENTS")
    result: dict[str, float] = {}
    for component in CREDIT_COMPONENTS:
        result[component] = _finite(value[component], f"{name}[{component}]", minimum=minimum, maximum=maximum)
    return result


def _confidence(value: Mapping[str, Any], name: str) -> dict[str, float]:
    return _components(value, name, minimum=0.0, maximum=1.0)


def _evidence(value: Any) -> str:
    text = _text(value, "evidence_level", maximum=32)
    if text not in EVIDENCE_LEVELS:
        raise ValueError(f"unsupported evidence_level: {text!r}")
    return text


def _origin(value: Any) -> str:
    text = _text(value, "data_origin", maximum=32)
    if text not in ALL_DATA_ORIGINS:
        raise ValueError(f"unsupported data_origin: {text!r}")
    return text


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative int")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _non_negative_int(value, name)
    if result < 1:
        raise ValueError(f"{name} must be >= 1")
    return result


def _exact_distribution(value: Mapping[str, Any], name: str) -> dict[str, float]:
    if not isinstance(value, Mapping) or not value:
        raise ValueError(f"{name} must be a non-empty mapping")
    result: dict[str, float] = {}
    for key, raw in value.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{name} keys must be non-empty strings")
        result[key] = probability(raw, f"{name}[{key}]")
    total = sum(result.values())
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"{name} must sum to 1 within 1e-9")
    return dict(sorted(result.items()))


def _reasons(value: Sequence[Any], name: str = "reason_codes") -> tuple[str, ...]:
    return unique_texts(value, name, maximum_items=64)


def _evidence_min(values: Sequence[str]) -> str:
    return min(values, key=lambda item: _EVIDENCE_RANK[item])


def _interval(values: Sequence[float]) -> tuple[float, float, float]:
    if not values:
        raise ValueError("values must not be empty")
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    margin = 1.96 * math.sqrt(variance) / math.sqrt(len(values))
    return (
        max(-1.0, min(1.0, mean)),
        max(-1.0, min(1.0, mean - margin)),
        max(-1.0, min(1.0, mean + margin)),
    )


@dataclass(frozen=True, slots=True)
class TraceCreditInputV1:
    project_id: str
    trace_id: str
    accepted: bool
    reward: float
    total_cost_usd: float
    total_latency_ms: int
    component_effects: Mapping[str, float]
    component_confidence: Mapping[str, float]
    evidence_level: str
    data_origin: str
    independently_validated: bool
    evidence_ids: tuple[str, ...]
    created_at: str
    schema_version: str = SAFE_LEARNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "trace_id", _text(self.trace_id, "trace_id"))
        object.__setattr__(self, "accepted", _bool(self.accepted, "accepted"))
        object.__setattr__(self, "reward", _finite(self.reward, "reward", minimum=-1.0, maximum=1.0))
        object.__setattr__(self, "total_cost_usd", finite_number(self.total_cost_usd, "total_cost_usd", minimum=0.0))
        object.__setattr__(self, "total_latency_ms", _non_negative_int(self.total_latency_ms, "total_latency_ms"))
        object.__setattr__(self, "component_effects", _components(self.component_effects, "component_effects", minimum=-1.0, maximum=1.0))
        object.__setattr__(self, "component_confidence", _confidence(self.component_confidence, "component_confidence"))
        object.__setattr__(self, "evidence_level", _evidence(self.evidence_level))
        object.__setattr__(self, "data_origin", _origin(self.data_origin))
        object.__setattr__(self, "independently_validated", _bool(self.independently_validated, "independently_validated"))
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=64))
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if self.schema_version != SAFE_LEARNING_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SAFE_LEARNING_SCHEMA_VERSION}")

    def identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "trace_id": self.trace_id,
            "accepted": self.accepted,
            "reward": self.reward,
            "total_cost_usd": self.total_cost_usd,
            "total_latency_ms": self.total_latency_ms,
            "component_effects": dict(self.component_effects),
            "component_confidence": dict(self.component_confidence),
            "evidence_level": self.evidence_level,
            "data_origin": self.data_origin,
            "independently_validated": self.independently_validated,
            "evidence_ids": list(self.evidence_ids),
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.identity()
        result["created_at"] = self.created_at
        result["trace_id"] = self.trace_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TraceCreditInputV1":
        required = frozenset({
            "project_id", "trace_id", "accepted", "reward", "total_cost_usd",
            "total_latency_ms", "component_effects", "component_confidence",
            "evidence_level", "data_origin", "independently_validated",
            "evidence_ids", "created_at", "schema_version",
        })
        validate_exact_fields(value, required=required)
        return cls(
            project_id=value["project_id"], trace_id=value["trace_id"], accepted=value["accepted"],
            reward=value["reward"], total_cost_usd=value["total_cost_usd"],
            total_latency_ms=value["total_latency_ms"], component_effects=value["component_effects"],
            component_confidence=value["component_confidence"], evidence_level=value["evidence_level"],
            data_origin=value["data_origin"], independently_validated=value["independently_validated"],
            evidence_ids=tuple(value["evidence_ids"]), created_at=value["created_at"],
            schema_version=value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class CreditAssignmentV1:
    project_id: str
    trace_id: str
    reward: float
    component_contributions: Mapping[str, float]
    residual_contribution: float
    component_confidence: Mapping[str, float]
    method: str
    evidence_level: str
    data_origin: str
    independently_validated: bool
    usable_for_learning: bool
    evidence_ids: tuple[str, ...]
    created_at: str
    policy_revision: str = SAFE_LEARNING_POLICY_REVISION
    schema_version: str = SAFE_LEARNING_SCHEMA_VERSION
    credit_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "trace_id", _text(self.trace_id, "trace_id"))
        object.__setattr__(self, "reward", _finite(self.reward, "reward", minimum=-1.0, maximum=1.0))
        object.__setattr__(self, "component_contributions", _components(self.component_contributions, "component_contributions", minimum=-1.0, maximum=1.0))
        object.__setattr__(self, "residual_contribution", _finite(self.residual_contribution, "residual_contribution", minimum=-2.0, maximum=2.0))
        object.__setattr__(self, "component_confidence", _confidence(self.component_confidence, "component_confidence"))
        if self.method not in CREDIT_METHODS:
            raise ValueError(f"unsupported credit method: {self.method!r}")
        object.__setattr__(self, "evidence_level", _evidence(self.evidence_level))
        object.__setattr__(self, "data_origin", _origin(self.data_origin))
        object.__setattr__(self, "independently_validated", _bool(self.independently_validated, "independently_validated"))
        object.__setattr__(self, "usable_for_learning", _bool(self.usable_for_learning, "usable_for_learning"))
        if self.usable_for_learning and (_EVIDENCE_RANK[self.evidence_level] < 2 or self.data_origin not in REAL_DATA_ORIGINS or not self.independently_validated):
            raise ValueError("usable_for_learning requires independently validated E2+ real data")
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=64))
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if self.policy_revision != SAFE_LEARNING_POLICY_REVISION:
            raise ValueError(f"policy_revision must be {SAFE_LEARNING_POLICY_REVISION}")
        if self.schema_version != SAFE_LEARNING_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SAFE_LEARNING_SCHEMA_VERSION}")
        expected = "credit065_" + stable_digest(self.identity(), length=32)
        if self.credit_id and self.credit_id != expected:
            raise ValueError("credit_id does not match content")
        object.__setattr__(self, "credit_id", expected)

    def identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "trace_id": self.trace_id, "reward": self.reward,
            "component_contributions": dict(self.component_contributions),
            "residual_contribution": self.residual_contribution,
            "component_confidence": dict(self.component_confidence), "method": self.method,
            "evidence_level": self.evidence_level, "data_origin": self.data_origin,
            "independently_validated": self.independently_validated,
            "usable_for_learning": self.usable_for_learning,
            "evidence_ids": list(self.evidence_ids), "policy_revision": self.policy_revision,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.identity()
        result.update({"credit_id": self.credit_id, "created_at": self.created_at})
        return result

    @classmethod
    def from_trace(cls, trace: TraceCreditInputV1, *, method: str = "deterministic") -> "CreditAssignmentV1":
        if method not in CREDIT_METHODS:
            raise ValueError(f"unsupported credit method: {method!r}")
        contributions = {
            component: trace.component_effects[component] * trace.component_confidence[component]
            for component in CREDIT_COMPONENTS
        }
        residual = trace.reward - sum(contributions.values())
        usable = (
            _EVIDENCE_RANK[trace.evidence_level] >= 2
            and trace.data_origin in REAL_DATA_ORIGINS
            and trace.independently_validated
            and method != "llm_attribution"
        )
        return cls(
            project_id=trace.project_id, trace_id=trace.trace_id, reward=trace.reward,
            component_contributions=contributions, residual_contribution=residual,
            component_confidence=trace.component_confidence, method=method,
            evidence_level=trace.evidence_level, data_origin=trace.data_origin,
            independently_validated=trace.independently_validated,
            usable_for_learning=usable, evidence_ids=trace.evidence_ids,
            created_at=trace.created_at,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CreditAssignmentV1":
        required = frozenset({
            "project_id", "trace_id", "reward", "component_contributions",
            "residual_contribution", "component_confidence", "method",
            "evidence_level", "data_origin", "independently_validated",
            "usable_for_learning", "evidence_ids", "created_at",
            "policy_revision", "schema_version",
        })
        validate_exact_fields(value, required=required, optional=frozenset({"credit_id"}))
        return cls(
            project_id=value["project_id"], trace_id=value["trace_id"], reward=value["reward"],
            component_contributions=value["component_contributions"],
            residual_contribution=value["residual_contribution"],
            component_confidence=value["component_confidence"], method=value["method"],
            evidence_level=value["evidence_level"], data_origin=value["data_origin"],
            independently_validated=value["independently_validated"],
            usable_for_learning=value["usable_for_learning"],
            evidence_ids=tuple(value["evidence_ids"]), created_at=value["created_at"],
            policy_revision=value["policy_revision"], schema_version=value["schema_version"],
            credit_id=value.get("credit_id", ""),
        )


@dataclass(frozen=True, slots=True)
class CounterfactualReplayV1:
    project_id: str
    trace_id: str
    replaced_component: str
    baseline_reward: float
    counterfactual_reward: float
    delta: float
    assumptions: tuple[str, ...]
    evidence_level: str
    deterministic: bool
    created_at: str
    schema_version: str = SAFE_LEARNING_SCHEMA_VERSION
    replay_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "trace_id", _text(self.trace_id, "trace_id"))
        object.__setattr__(self, "replaced_component", _text(self.replaced_component, "replaced_component", maximum=32))
        if self.replaced_component not in CREDIT_COMPONENTS:
            raise ValueError("replaced_component must be a credit component")
        object.__setattr__(self, "baseline_reward", _finite(self.baseline_reward, "baseline_reward", minimum=-1.0, maximum=1.0))
        object.__setattr__(self, "counterfactual_reward", _finite(self.counterfactual_reward, "counterfactual_reward", minimum=-1.0, maximum=1.0))
        delta = self.counterfactual_reward - self.baseline_reward
        object.__setattr__(self, "delta", _finite(self.delta, "delta", minimum=-2.0, maximum=2.0))
        if abs(self.delta - delta) > 1e-9:
            raise ValueError("delta must equal counterfactual_reward - baseline_reward")
        object.__setattr__(self, "assumptions", unique_texts(self.assumptions, "assumptions", maximum_items=32))
        object.__setattr__(self, "evidence_level", _evidence(self.evidence_level))
        object.__setattr__(self, "deterministic", _bool(self.deterministic, "deterministic"))
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if self.schema_version != SAFE_LEARNING_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SAFE_LEARNING_SCHEMA_VERSION}")
        expected = "replay065_" + stable_digest(self.identity(), length=32)
        if self.replay_id and self.replay_id != expected:
            raise ValueError("replay_id does not match content")
        object.__setattr__(self, "replay_id", expected)

    def identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "trace_id": self.trace_id,
            "replaced_component": self.replaced_component, "baseline_reward": self.baseline_reward,
            "counterfactual_reward": self.counterfactual_reward, "delta": self.delta,
            "assumptions": list(self.assumptions), "evidence_level": self.evidence_level,
            "deterministic": self.deterministic, "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.identity()
        result.update({"replay_id": self.replay_id, "created_at": self.created_at})
        return result

    @classmethod
    def replay(cls, trace: TraceCreditInputV1, replaced_component: str, alternative_effect: float, *, assumptions: Sequence[str] = ()) -> "CounterfactualReplayV1":
        if replaced_component not in CREDIT_COMPONENTS:
            raise ValueError("replaced_component must be a credit component")
        alternative = _finite(alternative_effect, "alternative_effect", minimum=-1.0, maximum=1.0)
        baseline = trace.reward
        counterfactual = _finite(baseline - trace.component_effects[replaced_component] + alternative, "counterfactual_reward", minimum=-1.0, maximum=1.0)
        return cls(
            project_id=trace.project_id, trace_id=trace.trace_id, replaced_component=replaced_component,
            baseline_reward=baseline, counterfactual_reward=counterfactual,
            delta=counterfactual - baseline, assumptions=tuple(assumptions),
            evidence_level="E0-simulated", deterministic=True, created_at=trace.created_at,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CounterfactualReplayV1":
        required = frozenset({
            "project_id", "trace_id", "replaced_component", "baseline_reward",
            "counterfactual_reward", "delta", "assumptions", "evidence_level",
            "deterministic", "created_at", "schema_version",
        })
        validate_exact_fields(value, required=required, optional=frozenset({"replay_id"}))
        return cls(
            project_id=value["project_id"], trace_id=value["trace_id"],
            replaced_component=value["replaced_component"], baseline_reward=value["baseline_reward"],
            counterfactual_reward=value["counterfactual_reward"], delta=value["delta"],
            assumptions=tuple(value["assumptions"]), evidence_level=value["evidence_level"],
            deterministic=value["deterministic"], created_at=value["created_at"],
            schema_version=value["schema_version"], replay_id=value.get("replay_id", ""),
        )


@dataclass(frozen=True, slots=True)
class LoggedDecisionV1:
    observation_id: str
    project_id: str
    task_family: str
    risk_level: str
    logged_action: str
    logging_action_probabilities: Mapping[str, float]
    candidate_action_probabilities: Mapping[str, float]
    predicted_rewards: Mapping[str, float]
    observed_reward: float
    evidence_level: str
    data_origin: str
    independently_validated: bool
    created_at: str
    schema_version: str = SAFE_LEARNING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "observation_id", _text(self.observation_id, "observation_id"))
        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "task_family", _text(self.task_family, "task_family", maximum=80))
        risk = _text(self.risk_level, "risk_level", maximum=16)
        if risk not in RISK_LEVELS:
            raise ValueError("unsupported risk_level")
        object.__setattr__(self, "risk_level", risk)
        object.__setattr__(self, "logged_action", _text(self.logged_action, "logged_action", maximum=80))
        object.__setattr__(self, "logging_action_probabilities", _exact_distribution(self.logging_action_probabilities, "logging_action_probabilities"))
        object.__setattr__(self, "candidate_action_probabilities", _exact_distribution(self.candidate_action_probabilities, "candidate_action_probabilities"))
        if self.logged_action not in self.logging_action_probabilities or self.logged_action not in self.candidate_action_probabilities:
            raise ValueError("logged_action must be present in both action distributions")
        if not isinstance(self.predicted_rewards, Mapping) or set(self.predicted_rewards) != set(self.candidate_action_probabilities):
            raise ValueError("predicted_rewards keys must match candidate actions")
        rewards = {key: _finite(raw, f"predicted_rewards[{key}]", minimum=-1.0, maximum=1.0) for key, raw in self.predicted_rewards.items()}
        object.__setattr__(self, "predicted_rewards", dict(sorted(rewards.items())))
        object.__setattr__(self, "observed_reward", _finite(self.observed_reward, "observed_reward", minimum=-1.0, maximum=1.0))
        object.__setattr__(self, "evidence_level", _evidence(self.evidence_level))
        object.__setattr__(self, "data_origin", _origin(self.data_origin))
        object.__setattr__(self, "independently_validated", _bool(self.independently_validated, "independently_validated"))
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if self.schema_version != SAFE_LEARNING_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SAFE_LEARNING_SCHEMA_VERSION}")

    @property
    def logging_propensity(self) -> float:
        return self.logging_action_probabilities[self.logged_action]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id, "project_id": self.project_id,
            "task_family": self.task_family, "risk_level": self.risk_level,
            "logged_action": self.logged_action,
            "logging_action_probabilities": dict(self.logging_action_probabilities),
            "candidate_action_probabilities": dict(self.candidate_action_probabilities),
            "predicted_rewards": dict(self.predicted_rewards),
            "observed_reward": self.observed_reward, "evidence_level": self.evidence_level,
            "data_origin": self.data_origin, "independently_validated": self.independently_validated,
            "created_at": self.created_at, "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LoggedDecisionV1":
        required = frozenset({
            "observation_id", "project_id", "task_family", "risk_level", "logged_action",
            "logging_action_probabilities", "candidate_action_probabilities",
            "predicted_rewards", "observed_reward", "evidence_level", "data_origin",
            "independently_validated", "created_at", "schema_version",
        })
        validate_exact_fields(value, required=required)
        return cls(
            observation_id=value["observation_id"], project_id=value["project_id"],
            task_family=value["task_family"], risk_level=value["risk_level"],
            logged_action=value["logged_action"],
            logging_action_probabilities=value["logging_action_probabilities"],
            candidate_action_probabilities=value["candidate_action_probabilities"],
            predicted_rewards=value["predicted_rewards"], observed_reward=value["observed_reward"],
            evidence_level=value["evidence_level"], data_origin=value["data_origin"],
            independently_validated=value["independently_validated"],
            created_at=value["created_at"], schema_version=value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class LearningEstimateV1:
    project_id: str
    policy_id: str
    policy_version: str
    task_family: str
    method: str
    estimate: float
    lower: float
    upper: float
    sample_count: int
    effective_sample_size: float
    max_weight: float
    common_support: bool
    calibration_ok: bool
    evidence_level: str
    usable_for_learning: bool
    reason_codes: tuple[str, ...]
    observation_ids: tuple[str, ...]
    created_at: str
    policy_revision: str = SAFE_LEARNING_POLICY_REVISION
    schema_version: str = SAFE_LEARNING_SCHEMA_VERSION
    estimate_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "policy_id", _text(self.policy_id, "policy_id", maximum=100))
        object.__setattr__(self, "policy_version", _text(self.policy_version, "policy_version", maximum=100))
        object.__setattr__(self, "task_family", _text(self.task_family, "task_family", maximum=80))
        if self.method not in ("ips", "dr"):
            raise ValueError("method must be ips or dr")
        object.__setattr__(self, "estimate", _finite(self.estimate, "estimate", minimum=-1.0, maximum=1.0))
        object.__setattr__(self, "lower", _finite(self.lower, "lower", minimum=-1.0, maximum=1.0))
        object.__setattr__(self, "upper", _finite(self.upper, "upper", minimum=-1.0, maximum=1.0))
        if self.lower > self.estimate or self.estimate > self.upper:
            raise ValueError("estimate interval must satisfy lower <= estimate <= upper")
        object.__setattr__(self, "sample_count", _non_negative_int(self.sample_count, "sample_count"))
        object.__setattr__(self, "effective_sample_size", finite_number(self.effective_sample_size, "effective_sample_size", minimum=0.0))
        object.__setattr__(self, "max_weight", finite_number(self.max_weight, "max_weight", minimum=0.0))
        object.__setattr__(self, "common_support", _bool(self.common_support, "common_support"))
        object.__setattr__(self, "calibration_ok", _bool(self.calibration_ok, "calibration_ok"))
        object.__setattr__(self, "evidence_level", _evidence(self.evidence_level))
        object.__setattr__(self, "usable_for_learning", _bool(self.usable_for_learning, "usable_for_learning"))
        object.__setattr__(self, "reason_codes", _reasons(self.reason_codes))
        object.__setattr__(self, "observation_ids", unique_texts(self.observation_ids, "observation_ids", maximum_items=10000))
        if self.sample_count != len(self.observation_ids):
            raise ValueError("sample_count must equal observation_ids length")
        if self.policy_revision != SAFE_LEARNING_POLICY_REVISION:
            raise ValueError(f"policy_revision must be {SAFE_LEARNING_POLICY_REVISION}")
        if self.schema_version != SAFE_LEARNING_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SAFE_LEARNING_SCHEMA_VERSION}")
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        expected = "estimate065_" + stable_digest(self.identity(), length=32)
        if self.estimate_id and self.estimate_id != expected:
            raise ValueError("estimate_id does not match content")
        object.__setattr__(self, "estimate_id", expected)

    def identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "policy_id": self.policy_id,
            "policy_version": self.policy_version, "task_family": self.task_family,
            "method": self.method, "estimate": self.estimate, "lower": self.lower,
            "upper": self.upper, "sample_count": self.sample_count,
            "effective_sample_size": self.effective_sample_size, "max_weight": self.max_weight,
            "common_support": self.common_support, "calibration_ok": self.calibration_ok,
            "evidence_level": self.evidence_level, "usable_for_learning": self.usable_for_learning,
            "reason_codes": list(self.reason_codes), "observation_ids": list(self.observation_ids),
            "policy_revision": self.policy_revision, "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.identity()
        result.update({"estimate_id": self.estimate_id, "created_at": self.created_at})
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "LearningEstimateV1":
        required = frozenset({
            "project_id", "policy_id", "policy_version", "task_family", "method",
            "estimate", "lower", "upper", "sample_count", "effective_sample_size",
            "max_weight", "common_support", "calibration_ok", "evidence_level",
            "usable_for_learning", "reason_codes", "observation_ids", "created_at",
            "policy_revision", "schema_version",
        })
        validate_exact_fields(value, required=required, optional=frozenset({"estimate_id"}))
        return cls(
            project_id=value["project_id"], policy_id=value["policy_id"],
            policy_version=value["policy_version"], task_family=value["task_family"],
            method=value["method"], estimate=value["estimate"], lower=value["lower"],
            upper=value["upper"], sample_count=value["sample_count"],
            effective_sample_size=value["effective_sample_size"], max_weight=value["max_weight"],
            common_support=value["common_support"], calibration_ok=value["calibration_ok"],
            evidence_level=value["evidence_level"], usable_for_learning=value["usable_for_learning"],
            reason_codes=tuple(value["reason_codes"]), observation_ids=tuple(value["observation_ids"]),
            created_at=value["created_at"], policy_revision=value["policy_revision"],
            schema_version=value["schema_version"], estimate_id=value.get("estimate_id", ""),
        )


@dataclass(frozen=True, slots=True)
class SafeLearningCandidateV1:
    project_id: str
    candidate_policy_id: str
    candidate_policy_version: str
    champion_policy_id: str
    champion_policy_version: str
    task_family: str
    estimate_id: str
    champion_estimate: float
    evidence_level: str
    rollback_policy_id: str
    status: str = "candidate"
    advisory_only: bool = True
    shadow_eligible: bool = False
    may_change_actual_route: bool = False
    automatic_promotion_allowed: bool = False
    commercial_claims_allowed: bool = False
    created_at: str = ""
    policy_revision: str = SAFE_LEARNING_POLICY_REVISION
    schema_version: str = SAFE_LEARNING_SCHEMA_VERSION
    candidate_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        for name in ("candidate_policy_id", "candidate_policy_version", "champion_policy_id", "champion_policy_version", "estimate_id", "rollback_policy_id"):
            object.__setattr__(self, name, _text(getattr(self, name), name, maximum=120))
        object.__setattr__(self, "task_family", _text(self.task_family, "task_family", maximum=80))
        object.__setattr__(self, "champion_estimate", _finite(self.champion_estimate, "champion_estimate", minimum=-1.0, maximum=1.0))
        object.__setattr__(self, "evidence_level", _evidence(self.evidence_level))
        if self.status not in LEARNING_STATES:
            raise ValueError("unsupported candidate status")
        for name in ("advisory_only", "shadow_eligible", "may_change_actual_route", "automatic_promotion_allowed", "commercial_claims_allowed"):
            object.__setattr__(self, name, _bool(getattr(self, name), name))
        if not self.advisory_only or self.may_change_actual_route or self.automatic_promotion_allowed or self.commercial_claims_allowed:
            raise ValueError("safe-learning candidates are always advisory-only")
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at or _now(), "created_at"))
        if self.policy_revision != SAFE_LEARNING_POLICY_REVISION:
            raise ValueError(f"policy_revision must be {SAFE_LEARNING_POLICY_REVISION}")
        if self.schema_version != SAFE_LEARNING_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SAFE_LEARNING_SCHEMA_VERSION}")
        expected = "safecandidate065_" + stable_digest(self.identity(), length=32)
        if self.candidate_id and self.candidate_id != expected:
            raise ValueError("candidate_id does not match content")
        object.__setattr__(self, "candidate_id", expected)

    def identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "candidate_policy_id": self.candidate_policy_id,
            "candidate_policy_version": self.candidate_policy_version,
            "champion_policy_id": self.champion_policy_id,
            "champion_policy_version": self.champion_policy_version, "task_family": self.task_family,
            "estimate_id": self.estimate_id, "champion_estimate": self.champion_estimate,
            "evidence_level": self.evidence_level, "rollback_policy_id": self.rollback_policy_id,
            "status": self.status, "advisory_only": self.advisory_only,
            "shadow_eligible": self.shadow_eligible, "may_change_actual_route": self.may_change_actual_route,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "policy_revision": self.policy_revision, "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.identity()
        result.update({"candidate_id": self.candidate_id, "created_at": self.created_at})
        return result

    @classmethod
    def create(cls, *, project_id: str, candidate_policy_id: str, candidate_policy_version: str, champion_policy_id: str, champion_policy_version: str, task_family: str, estimate: LearningEstimateV1, champion_estimate: float, rollback_policy_id: str) -> "SafeLearningCandidateV1":
        return cls(
            project_id=project_id, candidate_policy_id=candidate_policy_id,
            candidate_policy_version=candidate_policy_version, champion_policy_id=champion_policy_id,
            champion_policy_version=champion_policy_version, task_family=task_family,
            estimate_id=estimate.estimate_id, champion_estimate=champion_estimate,
            evidence_level=estimate.evidence_level, rollback_policy_id=rollback_policy_id,
            shadow_eligible=False,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SafeLearningCandidateV1":
        required = frozenset({
            "project_id", "candidate_policy_id", "candidate_policy_version",
            "champion_policy_id", "champion_policy_version", "task_family", "estimate_id",
            "champion_estimate", "evidence_level", "rollback_policy_id", "status",
            "advisory_only", "shadow_eligible", "may_change_actual_route",
            "automatic_promotion_allowed", "commercial_claims_allowed", "created_at",
            "policy_revision", "schema_version",
        })
        validate_exact_fields(value, required=required, optional=frozenset({"candidate_id"}))
        return cls(
            project_id=value["project_id"], candidate_policy_id=value["candidate_policy_id"],
            candidate_policy_version=value["candidate_policy_version"], champion_policy_id=value["champion_policy_id"],
            champion_policy_version=value["champion_policy_version"], task_family=value["task_family"],
            estimate_id=value["estimate_id"], champion_estimate=value["champion_estimate"],
            evidence_level=value["evidence_level"], rollback_policy_id=value["rollback_policy_id"],
            status=value["status"], advisory_only=value["advisory_only"],
            shadow_eligible=value["shadow_eligible"], may_change_actual_route=value["may_change_actual_route"],
            automatic_promotion_allowed=value["automatic_promotion_allowed"],
            commercial_claims_allowed=value["commercial_claims_allowed"], created_at=value["created_at"],
            policy_revision=value["policy_revision"], schema_version=value["schema_version"],
            candidate_id=value.get("candidate_id", ""),
        )


@dataclass(frozen=True, slots=True)
class PromotionAssessmentV1:
    project_id: str
    candidate_id: str
    estimate_id: str
    stage: str
    state: str
    hard_checks: Mapping[str, bool]
    candidate_lower: float
    champion_estimate: float
    lower_delta: float
    reason_codes: tuple[str, ...]
    rollback_policy_id: str
    advisory_only: bool = True
    route_mutation_allowed: bool = False
    automatic_promotion_allowed: bool = False
    commercial_claims_allowed: bool = False
    created_at: str = ""
    policy_revision: str = SAFE_LEARNING_POLICY_REVISION
    schema_version: str = SAFE_LEARNING_SCHEMA_VERSION
    assessment_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
        object.__setattr__(self, "candidate_id", _text(self.candidate_id, "candidate_id", maximum=120))
        object.__setattr__(self, "estimate_id", _text(self.estimate_id, "estimate_id", maximum=120))
        if self.stage not in LEARNING_STAGES:
            raise ValueError("unsupported assessment stage")
        if self.state not in LEARNING_STATES:
            raise ValueError("unsupported assessment state")
        required_checks = {"evidence_level", "common_support", "sample_size", "candidate_noninferior", "quality_guardrail", "budget_guardrail", "rollback_ready", "advisory_only"}
        if not isinstance(self.hard_checks, Mapping) or set(self.hard_checks) != required_checks:
            raise ValueError("hard_checks keys are invalid")
        object.__setattr__(self, "hard_checks", {key: _bool(self.hard_checks[key], f"hard_checks[{key}]") for key in sorted(required_checks)})
        object.__setattr__(self, "candidate_lower", _finite(self.candidate_lower, "candidate_lower", minimum=-1.0, maximum=1.0))
        object.__setattr__(self, "champion_estimate", _finite(self.champion_estimate, "champion_estimate", minimum=-1.0, maximum=1.0))
        expected_delta = self.candidate_lower - self.champion_estimate
        object.__setattr__(self, "lower_delta", _finite(self.lower_delta, "lower_delta", minimum=-2.0, maximum=2.0))
        if abs(self.lower_delta - expected_delta) > 1e-9:
            raise ValueError("lower_delta must equal candidate_lower - champion_estimate")
        object.__setattr__(self, "reason_codes", _reasons(self.reason_codes))
        object.__setattr__(self, "rollback_policy_id", _text(self.rollback_policy_id, "rollback_policy_id", maximum=120))
        for name in ("advisory_only", "route_mutation_allowed", "automatic_promotion_allowed", "commercial_claims_allowed"):
            object.__setattr__(self, name, _bool(getattr(self, name), name))
        if not self.advisory_only or self.route_mutation_allowed or self.automatic_promotion_allowed or self.commercial_claims_allowed:
            raise ValueError("promotion assessments cannot mutate live routing")
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at or _now(), "created_at"))
        if self.policy_revision != SAFE_LEARNING_POLICY_REVISION:
            raise ValueError(f"policy_revision must be {SAFE_LEARNING_POLICY_REVISION}")
        if self.schema_version != SAFE_LEARNING_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {SAFE_LEARNING_SCHEMA_VERSION}")
        expected = "assessment065_" + stable_digest(self.identity(), length=32)
        if self.assessment_id and self.assessment_id != expected:
            raise ValueError("assessment_id does not match content")
        object.__setattr__(self, "assessment_id", expected)

    def identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "candidate_id": self.candidate_id,
            "estimate_id": self.estimate_id, "stage": self.stage, "state": self.state,
            "hard_checks": dict(self.hard_checks), "candidate_lower": self.candidate_lower,
            "champion_estimate": self.champion_estimate, "lower_delta": self.lower_delta,
            "reason_codes": list(self.reason_codes), "rollback_policy_id": self.rollback_policy_id,
            "advisory_only": self.advisory_only, "route_mutation_allowed": self.route_mutation_allowed,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "policy_revision": self.policy_revision, "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        result = self.identity()
        result.update({"assessment_id": self.assessment_id, "created_at": self.created_at})
        return result

    @classmethod
    def assess_candidate(cls, candidate: SafeLearningCandidateV1, estimate: LearningEstimateV1, *, stage: str = "shadow") -> "PromotionAssessmentV1":
        if candidate.project_id != estimate.project_id:
            raise ValueError("candidate and estimate project mismatch")
        if candidate.estimate_id != estimate.estimate_id:
            raise ValueError("candidate estimate_id mismatch")
        if stage not in LEARNING_STAGES:
            raise ValueError("unsupported assessment stage")
        checks = {
            "evidence_level": _EVIDENCE_RANK[estimate.evidence_level] >= 2,
            "common_support": estimate.common_support,
            "sample_size": estimate.sample_count >= MIN_LEARNING_SAMPLES and estimate.effective_sample_size >= MIN_LEARNING_SAMPLES,
            "candidate_noninferior": estimate.lower >= candidate.champion_estimate - 0.02 and estimate.upper >= candidate.champion_estimate,
            "quality_guardrail": estimate.calibration_ok and estimate.max_weight <= MAX_IMPORTANCE_WEIGHT,
            "budget_guardrail": True,
            "rollback_ready": bool(candidate.rollback_policy_id),
            "advisory_only": candidate.advisory_only and not candidate.may_change_actual_route,
        }
        reasons = [key for key, value in checks.items() if not value]
        if not estimate.usable_for_learning:
            reasons.append("estimate_not_usable_for_learning")
        if stage == "offline" and not reasons:
            state = "frozen"
        elif not reasons and stage == "shadow":
            state = "shadow_eligible"
        else:
            state = "rejected"
        return cls(
            project_id=candidate.project_id, candidate_id=candidate.candidate_id,
            estimate_id=estimate.estimate_id, stage=stage, state=state, hard_checks=checks,
            candidate_lower=estimate.lower, champion_estimate=candidate.champion_estimate,
            lower_delta=estimate.lower - candidate.champion_estimate,
            reason_codes=tuple(reasons), rollback_policy_id=candidate.rollback_policy_id,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "PromotionAssessmentV1":
        required = frozenset({
            "project_id", "candidate_id", "estimate_id", "stage", "state", "hard_checks",
            "candidate_lower", "champion_estimate", "lower_delta", "reason_codes",
            "rollback_policy_id", "advisory_only", "route_mutation_allowed",
            "automatic_promotion_allowed", "commercial_claims_allowed", "created_at",
            "policy_revision", "schema_version",
        })
        validate_exact_fields(value, required=required, optional=frozenset({"assessment_id"}))
        return cls(
            project_id=value["project_id"], candidate_id=value["candidate_id"],
            estimate_id=value["estimate_id"], stage=value["stage"], state=value["state"],
            hard_checks=value["hard_checks"], candidate_lower=value["candidate_lower"],
            champion_estimate=value["champion_estimate"], lower_delta=value["lower_delta"],
            reason_codes=tuple(value["reason_codes"]), rollback_policy_id=value["rollback_policy_id"],
            advisory_only=value["advisory_only"], route_mutation_allowed=value["route_mutation_allowed"],
            automatic_promotion_allowed=value["automatic_promotion_allowed"],
            commercial_claims_allowed=value["commercial_claims_allowed"], created_at=value["created_at"],
            policy_revision=value["policy_revision"], schema_version=value["schema_version"],
            assessment_id=value.get("assessment_id", ""),
        )


def assign_credit(trace: TraceCreditInputV1, *, method: str = "deterministic") -> CreditAssignmentV1:
    return CreditAssignmentV1.from_trace(trace, method=method)


def counterfactual_replay(trace: TraceCreditInputV1, replaced_component: str, alternative_effect: float, *, assumptions: Sequence[str] = ()) -> CounterfactualReplayV1:
    return CounterfactualReplayV1.replay(trace, replaced_component, alternative_effect, assumptions=assumptions)


def estimate_policy(observations: Sequence[LoggedDecisionV1], *, policy_id: str, policy_version: str, task_family: str, method: str = "dr", created_at: str | None = None) -> LearningEstimateV1:
    if not observations:
        raise ValueError("observations must not be empty")
    if method not in ("ips", "dr"):
        raise ValueError("method must be ips or dr")
    task = _text(task_family, "task_family", maximum=80)
    selected = [item for item in observations if item.task_family == task]
    if not selected:
        raise ValueError("no observations match task_family")
    projects = {item.project_id for item in selected}
    if len(projects) != 1:
        raise ValueError("all selected observations must belong to the same project")
    values: list[float] = []
    weights: list[float] = []
    raw_weights: list[float] = []
    support = True
    calibration = True
    for item in selected:
        target_prob = item.candidate_action_probabilities.get(item.logged_action, 0.0)
        item_support = item.logging_propensity > 0.0 and all(
            candidate_probability <= 0.0
            or item.logging_action_probabilities.get(action, 0.0) > 0.0
            for action, candidate_probability in item.candidate_action_probabilities.items()
        )
        support = support and item_support
        raw_weight = (
            target_prob / item.logging_propensity
            if item.logging_propensity > 0.0
            else MAX_IMPORTANCE_WEIGHT + 1.0
        )
        raw_weights.append(raw_weight)
        weight = min(raw_weight, MAX_IMPORTANCE_WEIGHT)
        weights.append(weight)
        target_baseline = sum(item.candidate_action_probabilities[action] * item.predicted_rewards[action] for action in item.candidate_action_probabilities)
        logged_baseline = item.predicted_rewards[item.logged_action]
        values.append(weight * item.observed_reward if method == "ips" else target_baseline + weight * (item.observed_reward - logged_baseline))
        calibration = calibration and all(-1.0 <= reward <= 1.0 for reward in item.predicted_rewards.values())
    estimate, lower, upper = _interval(values)
    denom = sum(weight * weight for weight in weights)
    effective = (sum(weights) ** 2 / denom) if denom else 0.0
    # Preserve the uncapped diagnostic value.  The estimator may use clipped
    # weights for numerical stability, but promotion gates must see whether
    # clipping was required instead of comparing a cap to itself.
    max_weight = max(raw_weights) if raw_weights else 0.0
    evidence = _evidence_min([item.evidence_level for item in selected])
    all_independent = all(item.independently_validated for item in selected)
    all_real = all(item.data_origin in REAL_DATA_ORIGINS for item in selected)
    reasons: list[str] = []
    if _EVIDENCE_RANK[evidence] < 2:
        reasons.append("evidence_below_learning_floor")
    if not support:
        reasons.append("common_support_failed")
    if max_weight > MAX_IMPORTANCE_WEIGHT:
        reasons.append("importance_weight_exceeds_cap")
    if len(selected) < MIN_LEARNING_SAMPLES or effective < MIN_LEARNING_SAMPLES:
        reasons.append("sample_size_below_floor")
    if not calibration:
        reasons.append("calibration_failed")
    if not all_independent:
        reasons.append("independent_validation_missing")
    if not all_real:
        reasons.append("real_data_required")
    usable = not reasons
    return LearningEstimateV1(
        project_id=selected[0].project_id, policy_id=policy_id, policy_version=policy_version,
        task_family=task, method=method, estimate=estimate, lower=lower, upper=upper,
        sample_count=len(selected), effective_sample_size=effective, max_weight=max_weight,
        common_support=support, calibration_ok=calibration, evidence_level=evidence,
        usable_for_learning=usable, reason_codes=tuple(reasons),
        observation_ids=tuple(item.observation_id for item in selected),
        created_at=created_at or _now(),
    )


def _demo_observations() -> list[LoggedDecisionV1]:
    result: list[LoggedDecisionV1] = []
    for index in range(12):
        result.append(LoggedDecisionV1(
            observation_id=f"demo-observation-{index:02d}", project_id="demo",
            task_family="classification", risk_level="low", logged_action="terra",
            logging_action_probabilities={"luna": 0.25, "terra": 0.75},
            candidate_action_probabilities={"luna": 0.4, "terra": 0.6},
            predicted_rewards={"luna": 0.55, "terra": 0.75},
            observed_reward=0.7 + (index % 3) * 0.02,
            evidence_level="E0-simulated", data_origin="test_fixture",
            independently_validated=False, created_at="2026-01-01T00:00:00+00:00",
        ))
    return result


def run_demo() -> dict[str, Any]:
    trace = TraceCreditInputV1(
        project_id="demo", trace_id="demo-trace", accepted=True, reward=0.72,
        total_cost_usd=0.01, total_latency_ms=1200,
        component_effects={component: 0.1 for component in CREDIT_COMPONENTS},
        component_confidence={component: 0.8 for component in CREDIT_COMPONENTS},
        evidence_level="E0-simulated", data_origin="test_fixture",
        independently_validated=False, evidence_ids=("fixture",),
        created_at="2026-01-01T00:00:00+00:00",
    )
    credit = assign_credit(trace)
    replay = counterfactual_replay(trace, "routing", 0.35, assumptions=("isolated_component_effect",))
    estimate = estimate_policy(_demo_observations(), policy_id="demo-policy", policy_version="1", task_family="classification")
    candidate = SafeLearningCandidateV1.create(
        project_id="demo", candidate_policy_id="demo-policy", candidate_policy_version="1",
        champion_policy_id="champion", champion_policy_version="1", task_family="classification",
        estimate=estimate, champion_estimate=0.68, rollback_policy_id="rollback-demo",
    )
    assessment = PromotionAssessmentV1.assess_candidate(candidate, estimate, stage="shadow")
    return {
        "schema_version": SAFE_LEARNING_SCHEMA_VERSION,
        "policy_revision": SAFE_LEARNING_POLICY_REVISION,
        "credit": credit.to_dict(), "replay": replay.to_dict(),
        "estimate": estimate.to_dict(), "candidate": candidate.to_dict(),
        "assessment": assessment.to_dict(), "training_enabled": False,
        "route_intervention": False, "advisory_only": True,
        "provider_calls": 0, "network_calls": 0, "commercial_claims_allowed": False,
    }


__all__ = [
    "ALL_DATA_ORIGINS", "CREDIT_METHODS", "CounterfactualReplayV1",
    "CreditAssignmentV1", "LEARNING_STAGES", "LEARNING_STATES",
    "LearningEstimateV1", "LoggedDecisionV1", "MAX_IMPORTANCE_WEIGHT",
    "MIN_LEARNING_SAMPLES", "PromotionAssessmentV1", "REAL_DATA_ORIGINS",
    "SAFE_LEARNING_CONFIRM_TOKEN", "SAFE_LEARNING_POLICY_REVISION",
    "SAFE_LEARNING_SCHEMA_VERSION", "SafeLearningCandidateV1",
    "TraceCreditInputV1", "assign_credit", "counterfactual_replay",
    "estimate_policy", "run_demo",
]
