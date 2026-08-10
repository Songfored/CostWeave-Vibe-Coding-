"""Deterministic execution-time belief updates and economic recovery for v0.6.4.

The module is deliberately provider-free.  It consumes a frozen execution
snapshot, updates a conservative progress belief, and compares bounded
recovery actions.  It never executes a model, changes a plan, or mutates the
stable runtime route.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from ..contracts_v4 import utc_now
from .common import (
    NonNegativeEstimate,
    ProbabilityInterval,
    bounded_text,
    canonical_json,
    finite_number,
    probability,
    stable_digest,
    unique_texts,
    validate_exact_fields,
    validate_sha256,
    validate_timestamp,
)


EXECUTION_RECOVERY_SCHEMA_VERSION = "v0.6.4"
EXECUTION_RECOVERY_POLICY_REVISION = "execution-time-belief-economic-recovery-v1"
EXECUTION_ACTIONS = (
    "CONTINUE",
    "RETRY_NODE",
    "SWITCH_MODEL",
    "REQUEST_MISSING_INFO",
    "REPLAN_SUBGRAPH",
    "FULL_REPLAN",
    "STOP_AND_RETURN_PARTIAL",
)
EXECUTION_STAGES = ("node", "milestone", "final")
NODE_STATUSES = (
    "pending",
    "running",
    "validated",
    "rejected",
    "suspect",
    "blocked",
    "skipped",
)
DEPENDENCY_TYPES = ("hard", "validation", "soft")
TRIGGER_KINDS = (
    "node_result",
    "milestone",
    "final_validation",
    "failure",
    "budget_drift",
    "deadline_change",
    "resource_change",
    "price_change",
    "missing_information",
    "constraint_change",
)


def _non_negative_int(value: Any, name: str, *, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a non-negative integer")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} exceeds maximum {maximum}")
    return value


def _non_negative(value: Any, name: str) -> float:
    return finite_number(value, name, minimum=0.0)


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
    return value


def _mapping(value: Any, name: str, *, maximum_keys: int = 256) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{name} must be an object")
    if len(value) > maximum_keys:
        raise ValueError(f"{name} has too many keys")
    try:
        import json

        result = json.loads(canonical_json(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must contain finite JSON values") from exc
    if not isinstance(result, dict):
        raise TypeError(f"{name} must be an object")
    return result


def _float_map(value: Any, name: str) -> dict[str, float]:
    raw = _mapping(value, name)
    return {bounded_text(key, f"{name}.key"): probability(item, f"{name}.{key}") for key, item in raw.items()}


@dataclass(frozen=True, slots=True)
class ExecutionResourceBeliefV1:
    queue_depth: int
    available_slots: int
    price_multiplier: float
    provider_available: bool
    telemetry_age_ms: int
    resource_revision: str
    alternate_route_available: bool
    orthogonal_validator_available: bool
    schema_version: str = EXECUTION_RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "queue_depth", _non_negative_int(self.queue_depth, "queue_depth", maximum=1_000_000))
        object.__setattr__(self, "available_slots", _non_negative_int(self.available_slots, "available_slots", maximum=1_000_000))
        object.__setattr__(self, "price_multiplier", _non_negative(self.price_multiplier, "price_multiplier"))
        object.__setattr__(self, "provider_available", _bool(self.provider_available, "provider_available"))
        object.__setattr__(self, "telemetry_age_ms", _non_negative_int(self.telemetry_age_ms, "telemetry_age_ms", maximum=31_536_000_000))
        object.__setattr__(self, "resource_revision", bounded_text(self.resource_revision, "resource_revision", maximum=200))
        object.__setattr__(self, "alternate_route_available", _bool(self.alternate_route_available, "alternate_route_available"))
        object.__setattr__(self, "orthogonal_validator_available", _bool(self.orthogonal_validator_available, "orthogonal_validator_available"))
        if self.schema_version != EXECUTION_RECOVERY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {EXECUTION_RECOVERY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "queue_depth": self.queue_depth,
            "available_slots": self.available_slots,
            "price_multiplier": self.price_multiplier,
            "provider_available": self.provider_available,
            "telemetry_age_ms": self.telemetry_age_ms,
            "resource_revision": self.resource_revision,
            "alternate_route_available": self.alternate_route_available,
            "orthogonal_validator_available": self.orthogonal_validator_available,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionResourceBeliefV1":
        validate_exact_fields(
            value,
            required=frozenset({
                "queue_depth", "available_slots", "price_multiplier", "provider_available",
                "telemetry_age_ms", "resource_revision", "alternate_route_available",
                "orthogonal_validator_available", "schema_version",
            }),
        )
        return cls(**{key: value[key] for key in (
            "queue_depth", "available_slots", "price_multiplier", "provider_available",
            "telemetry_age_ms", "resource_revision", "alternate_route_available",
            "orthogonal_validator_available", "schema_version",
        )})


@dataclass(frozen=True, slots=True)
class ExecutionNodeStateV1:
    node_id: str
    dependencies: tuple[str, ...]
    dependency_types: Mapping[str, str]
    status: str
    predicted_success: ProbabilityInterval
    predicted_cost_usd: NonNegativeEstimate
    predicted_latency_ms: NonNegativeEstimate
    actual_cost_usd: float
    actual_latency_ms: int
    attempts: int
    max_attempts: int
    artifact_refs: tuple[str, ...]
    accepted_artifact_refs: tuple[str, ...]
    reusable_artifact_refs: tuple[str, ...]
    failure_class: str | None
    validation_score: float | None
    validator_family: str
    alternate_executor_available: bool
    criticality: float
    schema_version: str = EXECUTION_RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", bounded_text(self.node_id, "node_id", maximum=200))
        object.__setattr__(self, "dependencies", unique_texts(self.dependencies, "dependencies", maximum_items=256))
        raw_types = _mapping(self.dependency_types, "dependency_types")
        normalized_types: dict[str, str] = {}
        for key, value in raw_types.items():
            key = bounded_text(key, "dependency_types.key", maximum=200)
            value = bounded_text(value, "dependency_types.value", maximum=32)
            if value not in DEPENDENCY_TYPES:
                raise ValueError(f"unsupported dependency type: {value!r}")
            normalized_types[key] = value
        if set(normalized_types) - set(self.dependencies):
            raise ValueError("dependency_types contains an unknown dependency")
        object.__setattr__(self, "dependency_types", dict(sorted(normalized_types.items())))
        if self.status not in NODE_STATUSES:
            raise ValueError(f"unsupported node status: {self.status!r}")
        if not isinstance(self.predicted_success, ProbabilityInterval):
            raise TypeError("predicted_success must be ProbabilityInterval")
        if not isinstance(self.predicted_cost_usd, NonNegativeEstimate):
            raise TypeError("predicted_cost_usd must be NonNegativeEstimate")
        if not isinstance(self.predicted_latency_ms, NonNegativeEstimate):
            raise TypeError("predicted_latency_ms must be NonNegativeEstimate")
        object.__setattr__(self, "actual_cost_usd", _non_negative(self.actual_cost_usd, "actual_cost_usd"))
        object.__setattr__(self, "actual_latency_ms", _non_negative_int(self.actual_latency_ms, "actual_latency_ms"))
        object.__setattr__(self, "attempts", _non_negative_int(self.attempts, "attempts", maximum=10_000))
        object.__setattr__(self, "max_attempts", _non_negative_int(self.max_attempts, "max_attempts", maximum=10_000))
        if self.max_attempts < self.attempts:
            raise ValueError("max_attempts cannot be below attempts")
        for field in ("artifact_refs", "accepted_artifact_refs", "reusable_artifact_refs"):
            object.__setattr__(self, field, unique_texts(getattr(self, field), field, maximum_items=512))
        if self.failure_class is not None:
            object.__setattr__(self, "failure_class", bounded_text(self.failure_class, "failure_class", maximum=120))
        if self.validation_score is not None:
            object.__setattr__(self, "validation_score", probability(self.validation_score, "validation_score"))
        object.__setattr__(self, "validator_family", bounded_text(self.validator_family, "validator_family", maximum=120))
        object.__setattr__(self, "alternate_executor_available", _bool(self.alternate_executor_available, "alternate_executor_available"))
        object.__setattr__(self, "criticality", _non_negative(self.criticality, "criticality"))
        if self.schema_version != EXECUTION_RECOVERY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {EXECUTION_RECOVERY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "dependencies": list(self.dependencies),
            "dependency_types": dict(self.dependency_types),
            "status": self.status,
            "predicted_success": self.predicted_success.to_dict(),
            "predicted_cost_usd": self.predicted_cost_usd.to_dict(),
            "predicted_latency_ms": self.predicted_latency_ms.to_dict(),
            "actual_cost_usd": self.actual_cost_usd,
            "actual_latency_ms": self.actual_latency_ms,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "artifact_refs": list(self.artifact_refs),
            "accepted_artifact_refs": list(self.accepted_artifact_refs),
            "reusable_artifact_refs": list(self.reusable_artifact_refs),
            "failure_class": self.failure_class,
            "validation_score": self.validation_score,
            "validator_family": self.validator_family,
            "alternate_executor_available": self.alternate_executor_available,
            "criticality": self.criticality,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionNodeStateV1":
        validate_exact_fields(
            value,
            required=frozenset({
                "node_id", "dependencies", "dependency_types", "status", "predicted_success",
                "predicted_cost_usd", "predicted_latency_ms", "actual_cost_usd", "actual_latency_ms",
                "attempts", "max_attempts", "artifact_refs", "accepted_artifact_refs",
                "reusable_artifact_refs", "failure_class", "validation_score", "validator_family",
                "alternate_executor_available", "criticality", "schema_version",
            }),
        )
        return cls(
            node_id=value["node_id"], dependencies=tuple(value["dependencies"]),
            dependency_types=value["dependency_types"], status=value["status"],
            predicted_success=ProbabilityInterval.from_dict(value["predicted_success"]),
            predicted_cost_usd=NonNegativeEstimate.from_dict(value["predicted_cost_usd"]),
            predicted_latency_ms=NonNegativeEstimate.from_dict(value["predicted_latency_ms"]),
            actual_cost_usd=value["actual_cost_usd"], actual_latency_ms=value["actual_latency_ms"],
            attempts=value["attempts"], max_attempts=value["max_attempts"],
            artifact_refs=tuple(value["artifact_refs"]),
            accepted_artifact_refs=tuple(value["accepted_artifact_refs"]),
            reusable_artifact_refs=tuple(value["reusable_artifact_refs"]),
            failure_class=value["failure_class"], validation_score=value["validation_score"],
            validator_family=value["validator_family"],
            alternate_executor_available=value["alternate_executor_available"],
            criticality=value["criticality"], schema_version=value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class ExecutionSnapshotV1:
    project_id: str
    case_id: str
    objective_hash: str
    plan_revision: int
    stage: str
    nodes: tuple[ExecutionNodeStateV1, ...]
    budget_remaining_usd: float
    deadline_remaining_ms: int
    spent_cost_usd: float
    spent_latency_ms: int
    validated_artifact_ids: tuple[str, ...]
    reusable_artifact_ids: tuple[str, ...]
    polluted_node_ids: tuple[str, ...]
    failure_node_id: str | None
    failure_class: str | None
    failure_confidence: float
    required_info_missing: tuple[str, ...]
    trigger_kinds: tuple[str, ...]
    resource: ExecutionResourceBeliefV1
    risk_loss_usd: float
    max_retries: int
    max_switches: int
    max_subgraph_replans: int
    max_full_replans: int
    observed_at: str
    evidence_ids: tuple[str, ...]
    schema_version: str = EXECUTION_RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", bounded_text(self.project_id, "project_id", maximum=200))
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        object.__setattr__(self, "objective_hash", validate_sha256(self.objective_hash, "objective_hash"))
        object.__setattr__(self, "plan_revision", _non_negative_int(self.plan_revision, "plan_revision"))
        if self.stage not in EXECUTION_STAGES:
            raise ValueError(f"unsupported stage: {self.stage!r}")
        if not self.nodes:
            raise ValueError("nodes must not be empty")
        nodes = tuple(self.nodes)
        if any(not isinstance(item, ExecutionNodeStateV1) for item in nodes):
            raise TypeError("nodes must contain ExecutionNodeStateV1")
        if len({item.node_id for item in nodes}) != len(nodes):
            raise ValueError("node IDs must be unique")
        known = {item.node_id for item in nodes}
        if any(dep not in known for item in nodes for dep in item.dependencies):
            raise ValueError("node dependency references an unknown node")
        object.__setattr__(self, "nodes", tuple(sorted(nodes, key=lambda item: item.node_id)))
        object.__setattr__(self, "budget_remaining_usd", _non_negative(self.budget_remaining_usd, "budget_remaining_usd"))
        object.__setattr__(self, "deadline_remaining_ms", _non_negative_int(self.deadline_remaining_ms, "deadline_remaining_ms"))
        object.__setattr__(self, "spent_cost_usd", _non_negative(self.spent_cost_usd, "spent_cost_usd"))
        object.__setattr__(self, "spent_latency_ms", _non_negative_int(self.spent_latency_ms, "spent_latency_ms"))
        for field in ("validated_artifact_ids", "reusable_artifact_ids", "polluted_node_ids", "required_info_missing", "evidence_ids"):
            object.__setattr__(self, field, unique_texts(getattr(self, field), field, maximum_items=1000))
        if self.failure_node_id is not None:
            object.__setattr__(self, "failure_node_id", bounded_text(self.failure_node_id, "failure_node_id", maximum=200))
            if self.failure_node_id not in known:
                raise ValueError("failure_node_id must refer to a known node")
        if self.failure_class is not None:
            object.__setattr__(self, "failure_class", bounded_text(self.failure_class, "failure_class", maximum=120))
        object.__setattr__(self, "failure_confidence", probability(self.failure_confidence, "failure_confidence"))
        trigger_kinds = unique_texts(self.trigger_kinds, "trigger_kinds", maximum_items=64)
        if any(item not in TRIGGER_KINDS for item in trigger_kinds):
            raise ValueError("unsupported trigger kind")
        object.__setattr__(self, "trigger_kinds", trigger_kinds)
        if not isinstance(self.resource, ExecutionResourceBeliefV1):
            raise TypeError("resource must be ExecutionResourceBeliefV1")
        object.__setattr__(self, "risk_loss_usd", _non_negative(self.risk_loss_usd, "risk_loss_usd"))
        for field in ("max_retries", "max_switches", "max_subgraph_replans", "max_full_replans"):
            object.__setattr__(self, field, _non_negative_int(getattr(self, field), field, maximum=1000))
        object.__setattr__(self, "observed_at", validate_timestamp(self.observed_at, "observed_at"))
        if self.schema_version != EXECUTION_RECOVERY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {EXECUTION_RECOVERY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "case_id": self.case_id,
            "objective_hash": self.objective_hash, "plan_revision": self.plan_revision,
            "stage": self.stage, "nodes": [item.to_dict() for item in self.nodes],
            "budget_remaining_usd": self.budget_remaining_usd,
            "deadline_remaining_ms": self.deadline_remaining_ms,
            "spent_cost_usd": self.spent_cost_usd, "spent_latency_ms": self.spent_latency_ms,
            "validated_artifact_ids": list(self.validated_artifact_ids),
            "reusable_artifact_ids": list(self.reusable_artifact_ids),
            "polluted_node_ids": list(self.polluted_node_ids),
            "failure_node_id": self.failure_node_id, "failure_class": self.failure_class,
            "failure_confidence": self.failure_confidence,
            "required_info_missing": list(self.required_info_missing),
            "trigger_kinds": list(self.trigger_kinds), "resource": self.resource.to_dict(),
            "risk_loss_usd": self.risk_loss_usd, "max_retries": self.max_retries,
            "max_switches": self.max_switches, "max_subgraph_replans": self.max_subgraph_replans,
            "max_full_replans": self.max_full_replans, "observed_at": self.observed_at,
            "evidence_ids": list(self.evidence_ids), "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionSnapshotV1":
        validate_exact_fields(
            value,
            required=frozenset({
                "project_id", "case_id", "objective_hash", "plan_revision", "stage", "nodes",
                "budget_remaining_usd", "deadline_remaining_ms", "spent_cost_usd", "spent_latency_ms",
                "validated_artifact_ids", "reusable_artifact_ids", "polluted_node_ids", "failure_node_id",
                "failure_class", "failure_confidence", "required_info_missing", "trigger_kinds", "resource",
                "risk_loss_usd", "max_retries", "max_switches", "max_subgraph_replans", "max_full_replans",
                "observed_at", "evidence_ids", "schema_version",
            }),
        )
        return cls(
            project_id=value["project_id"], case_id=value["case_id"], objective_hash=value["objective_hash"],
            plan_revision=value["plan_revision"], stage=value["stage"],
            nodes=tuple(ExecutionNodeStateV1.from_dict(item) for item in value["nodes"]),
            budget_remaining_usd=value["budget_remaining_usd"], deadline_remaining_ms=value["deadline_remaining_ms"],
            spent_cost_usd=value["spent_cost_usd"], spent_latency_ms=value["spent_latency_ms"],
            validated_artifact_ids=tuple(value["validated_artifact_ids"]),
            reusable_artifact_ids=tuple(value["reusable_artifact_ids"]),
            polluted_node_ids=tuple(value["polluted_node_ids"]), failure_node_id=value["failure_node_id"],
            failure_class=value["failure_class"], failure_confidence=value["failure_confidence"],
            required_info_missing=tuple(value["required_info_missing"]), trigger_kinds=tuple(value["trigger_kinds"]),
            resource=ExecutionResourceBeliefV1.from_dict(value["resource"]), risk_loss_usd=value["risk_loss_usd"],
            max_retries=value["max_retries"], max_switches=value["max_switches"],
            max_subgraph_replans=value["max_subgraph_replans"], max_full_replans=value["max_full_replans"],
            observed_at=value["observed_at"], evidence_ids=tuple(value["evidence_ids"]), schema_version=value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class ProgressBeliefV1:
    project_id: str
    case_id: str
    plan_revision: int
    stage: str
    success_probability: ProbabilityInterval
    node_success_probabilities: Mapping[str, float]
    validated_nodes: int
    rejected_nodes: int
    active_nodes: int
    remaining_nodes: int
    critical_path_remaining_ms: int
    remaining_budget_usd: float
    deadline_remaining_ms: int
    artifact_acceptance: Mapping[str, int]
    pollution_scope: tuple[str, ...]
    resource_pressure: float
    validator_diversity: float
    sunk_cost_excluded_usd: float
    reusable_artifact_savings_usd: float
    trigger_reasons: tuple[str, ...]
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    observed_at: str
    schema_version: str = EXECUTION_RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", bounded_text(self.project_id, "project_id", maximum=200))
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        object.__setattr__(self, "plan_revision", _non_negative_int(self.plan_revision, "plan_revision"))
        if self.stage not in EXECUTION_STAGES:
            raise ValueError(f"unsupported stage: {self.stage!r}")
        if not isinstance(self.success_probability, ProbabilityInterval):
            raise TypeError("success_probability must be ProbabilityInterval")
        object.__setattr__(self, "node_success_probabilities", _float_map(self.node_success_probabilities, "node_success_probabilities"))
        for field in ("validated_nodes", "rejected_nodes", "active_nodes", "remaining_nodes", "critical_path_remaining_ms", "deadline_remaining_ms"):
            object.__setattr__(self, field, _non_negative_int(getattr(self, field), field))
        object.__setattr__(self, "remaining_budget_usd", _non_negative(self.remaining_budget_usd, "remaining_budget_usd"))
        artifact = _mapping(self.artifact_acceptance, "artifact_acceptance")
        if any(not isinstance(value, int) or isinstance(value, bool) or value < 0 for value in artifact.values()):
            raise ValueError("artifact_acceptance values must be non-negative integers")
        object.__setattr__(self, "artifact_acceptance", {str(key): int(value) for key, value in artifact.items()})
        object.__setattr__(self, "pollution_scope", unique_texts(self.pollution_scope, "pollution_scope", maximum_items=1000))
        object.__setattr__(self, "resource_pressure", probability(self.resource_pressure, "resource_pressure"))
        object.__setattr__(self, "validator_diversity", probability(self.validator_diversity, "validator_diversity"))
        object.__setattr__(self, "sunk_cost_excluded_usd", _non_negative(self.sunk_cost_excluded_usd, "sunk_cost_excluded_usd"))
        object.__setattr__(self, "reusable_artifact_savings_usd", _non_negative(self.reusable_artifact_savings_usd, "reusable_artifact_savings_usd"))
        object.__setattr__(self, "trigger_reasons", unique_texts(self.trigger_reasons, "trigger_reasons", maximum_items=128))
        object.__setattr__(self, "reason_codes", unique_texts(self.reason_codes, "reason_codes", maximum_items=128))
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=256))
        object.__setattr__(self, "observed_at", validate_timestamp(self.observed_at, "observed_at"))
        if self.schema_version != EXECUTION_RECOVERY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {EXECUTION_RECOVERY_SCHEMA_VERSION}")

    @property
    def belief_id(self) -> str:
        return "progress_belief_" + stable_digest(self._identity(), length=32)

    def _identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "case_id": self.case_id,
            "plan_revision": self.plan_revision, "stage": self.stage,
            "success_probability": self.success_probability.to_dict(),
            "node_success_probabilities": dict(sorted(self.node_success_probabilities.items())),
            "validated_nodes": self.validated_nodes, "rejected_nodes": self.rejected_nodes,
            "active_nodes": self.active_nodes, "remaining_nodes": self.remaining_nodes,
            "critical_path_remaining_ms": self.critical_path_remaining_ms,
            "remaining_budget_usd": self.remaining_budget_usd,
            "deadline_remaining_ms": self.deadline_remaining_ms,
            "artifact_acceptance": dict(sorted(self.artifact_acceptance.items())),
            "pollution_scope": list(self.pollution_scope), "resource_pressure": self.resource_pressure,
            "validator_diversity": self.validator_diversity,
            "sunk_cost_excluded_usd": self.sunk_cost_excluded_usd,
            "reusable_artifact_savings_usd": self.reusable_artifact_savings_usd,
            "trigger_reasons": list(self.trigger_reasons), "reason_codes": list(self.reason_codes),
            "evidence_ids": list(self.evidence_ids), "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            **self._identity(), "observed_at": self.observed_at, "belief_id": self.belief_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProgressBeliefV1":
        validate_exact_fields(
            value,
            required=frozenset({
                "project_id", "case_id", "plan_revision", "stage", "success_probability",
                "node_success_probabilities", "validated_nodes", "rejected_nodes", "active_nodes",
                "remaining_nodes", "critical_path_remaining_ms", "remaining_budget_usd",
                "deadline_remaining_ms", "artifact_acceptance", "pollution_scope", "resource_pressure",
                "validator_diversity", "sunk_cost_excluded_usd", "reusable_artifact_savings_usd",
                "trigger_reasons", "reason_codes", "evidence_ids", "observed_at", "schema_version",
            }),
            optional=frozenset({"belief_id"}),
        )
        item = cls(
            project_id=value["project_id"], case_id=value["case_id"], plan_revision=value["plan_revision"], stage=value["stage"],
            success_probability=ProbabilityInterval.from_dict(value["success_probability"]),
            node_success_probabilities=value["node_success_probabilities"], validated_nodes=value["validated_nodes"],
            rejected_nodes=value["rejected_nodes"], active_nodes=value["active_nodes"], remaining_nodes=value["remaining_nodes"],
            critical_path_remaining_ms=value["critical_path_remaining_ms"], remaining_budget_usd=value["remaining_budget_usd"],
            deadline_remaining_ms=value["deadline_remaining_ms"], artifact_acceptance=value["artifact_acceptance"],
            pollution_scope=tuple(value["pollution_scope"]), resource_pressure=value["resource_pressure"],
            validator_diversity=value["validator_diversity"], sunk_cost_excluded_usd=value["sunk_cost_excluded_usd"],
            reusable_artifact_savings_usd=value["reusable_artifact_savings_usd"], trigger_reasons=tuple(value["trigger_reasons"]),
            reason_codes=tuple(value["reason_codes"]), evidence_ids=tuple(value["evidence_ids"]),
            observed_at=value["observed_at"], schema_version=value["schema_version"],
        )
        if "belief_id" in value and value["belief_id"] != item.belief_id:
            raise ValueError("belief_id does not match content")
        return item


@dataclass(frozen=True, slots=True)
class RecoveryOptionV1:
    action: str
    feasible: bool
    target_node_ids: tuple[str, ...]
    validator_family: str
    expected_success: ProbabilityInterval
    incremental_cost_usd: NonNegativeEstimate
    expected_latency_ms: NonNegativeEstimate
    switching_cost_usd: float
    artifact_reuse_savings_usd: float
    sunk_cost_excluded_usd: float
    expected_loss_usd: float
    cps: float
    hard_checks: Mapping[str, bool]
    reason_codes: tuple[str, ...]
    schema_version: str = EXECUTION_RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.action not in EXECUTION_ACTIONS:
            raise ValueError(f"unsupported recovery action: {self.action!r}")
        object.__setattr__(self, "feasible", _bool(self.feasible, "feasible"))
        object.__setattr__(self, "target_node_ids", unique_texts(self.target_node_ids, "target_node_ids", maximum_items=1000))
        object.__setattr__(self, "validator_family", bounded_text(self.validator_family, "validator_family", maximum=120))
        if not isinstance(self.expected_success, ProbabilityInterval):
            raise TypeError("expected_success must be ProbabilityInterval")
        if not isinstance(self.incremental_cost_usd, NonNegativeEstimate):
            raise TypeError("incremental_cost_usd must be NonNegativeEstimate")
        if not isinstance(self.expected_latency_ms, NonNegativeEstimate):
            raise TypeError("expected_latency_ms must be NonNegativeEstimate")
        for field in ("switching_cost_usd", "artifact_reuse_savings_usd", "sunk_cost_excluded_usd", "expected_loss_usd", "cps"):
            object.__setattr__(self, field, _non_negative(getattr(self, field), field))
        checks = _mapping(self.hard_checks, "hard_checks")
        if any(not isinstance(value, bool) for value in checks.values()):
            raise TypeError("hard_checks values must be bool")
        object.__setattr__(self, "hard_checks", {str(key): bool(value) for key, value in checks.items()})
        object.__setattr__(self, "reason_codes", unique_texts(self.reason_codes, "reason_codes", maximum_items=128))
        if self.schema_version != EXECUTION_RECOVERY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {EXECUTION_RECOVERY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "feasible": self.feasible, "target_node_ids": list(self.target_node_ids),
            "validator_family": self.validator_family, "expected_success": self.expected_success.to_dict(),
            "incremental_cost_usd": self.incremental_cost_usd.to_dict(), "expected_latency_ms": self.expected_latency_ms.to_dict(),
            "switching_cost_usd": self.switching_cost_usd, "artifact_reuse_savings_usd": self.artifact_reuse_savings_usd,
            "sunk_cost_excluded_usd": self.sunk_cost_excluded_usd, "expected_loss_usd": self.expected_loss_usd,
            "cps": self.cps, "hard_checks": dict(sorted(self.hard_checks.items())),
            "reason_codes": list(self.reason_codes), "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RecoveryOptionV1":
        validate_exact_fields(
            value,
            required=frozenset({
                "action", "feasible", "target_node_ids", "validator_family", "expected_success",
                "incremental_cost_usd", "expected_latency_ms", "switching_cost_usd",
                "artifact_reuse_savings_usd", "sunk_cost_excluded_usd", "expected_loss_usd", "cps",
                "hard_checks", "reason_codes", "schema_version",
            }),
        )
        return cls(
            action=value["action"], feasible=value["feasible"], target_node_ids=tuple(value["target_node_ids"]),
            validator_family=value["validator_family"], expected_success=ProbabilityInterval.from_dict(value["expected_success"]),
            incremental_cost_usd=NonNegativeEstimate.from_dict(value["incremental_cost_usd"]),
            expected_latency_ms=NonNegativeEstimate.from_dict(value["expected_latency_ms"]),
            switching_cost_usd=value["switching_cost_usd"], artifact_reuse_savings_usd=value["artifact_reuse_savings_usd"],
            sunk_cost_excluded_usd=value["sunk_cost_excluded_usd"], expected_loss_usd=value["expected_loss_usd"],
            cps=value["cps"], hard_checks=value["hard_checks"], reason_codes=tuple(value["reason_codes"]),
            schema_version=value["schema_version"],
        )


@dataclass(frozen=True, slots=True)
class ExecutionRecoveryDecisionV1:
    project_id: str
    case_id: str
    stage: str
    action: str
    belief: ProgressBeliefV1
    options: tuple[RecoveryOptionV1, ...]
    baseline_cps: Mapping[str, float]
    recovery_advantage_usd: float
    decision_confidence: float
    trigger_reasons: tuple[str, ...]
    reason_codes: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    advisory_only: bool = True
    production_route_intervention: bool = False
    provider_calls: int = 0
    network_calls: int = 0
    decision_id: str = ""
    trace_hash: str = ""
    policy_revision: str = EXECUTION_RECOVERY_POLICY_REVISION
    schema_version: str = EXECUTION_RECOVERY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", bounded_text(self.project_id, "project_id", maximum=200))
        object.__setattr__(self, "case_id", bounded_text(self.case_id, "case_id", maximum=200))
        if self.stage not in EXECUTION_STAGES:
            raise ValueError(f"unsupported stage: {self.stage!r}")
        if self.action not in EXECUTION_ACTIONS:
            raise ValueError(f"unsupported recovery action: {self.action!r}")
        if not isinstance(self.belief, ProgressBeliefV1):
            raise TypeError("belief must be ProgressBeliefV1")
        if not self.options or any(not isinstance(item, RecoveryOptionV1) for item in self.options):
            raise ValueError("options must contain RecoveryOptionV1")
        object.__setattr__(self, "options", tuple(self.options))
        object.__setattr__(self, "baseline_cps", {bounded_text(key, "baseline_cps.key", maximum=64): _non_negative(value, "baseline_cps.value") for key, value in _mapping(self.baseline_cps, "baseline_cps").items()})
        object.__setattr__(self, "recovery_advantage_usd", _non_negative(self.recovery_advantage_usd, "recovery_advantage_usd"))
        object.__setattr__(self, "decision_confidence", probability(self.decision_confidence, "decision_confidence"))
        object.__setattr__(self, "trigger_reasons", unique_texts(self.trigger_reasons, "trigger_reasons", maximum_items=128))
        object.__setattr__(self, "reason_codes", unique_texts(self.reason_codes, "reason_codes", maximum_items=128))
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=256))
        object.__setattr__(self, "advisory_only", _bool(self.advisory_only, "advisory_only"))
        object.__setattr__(self, "production_route_intervention", _bool(self.production_route_intervention, "production_route_intervention"))
        object.__setattr__(self, "provider_calls", _non_negative_int(self.provider_calls, "provider_calls"))
        object.__setattr__(self, "network_calls", _non_negative_int(self.network_calls, "network_calls"))
        if not self.advisory_only or self.production_route_intervention or self.provider_calls or self.network_calls:
            raise ValueError("v0.6.4 decisions must remain offline advisory-only")
        if self.policy_revision != EXECUTION_RECOVERY_POLICY_REVISION:
            raise ValueError(f"policy_revision must be {EXECUTION_RECOVERY_POLICY_REVISION}")
        if self.schema_version != EXECUTION_RECOVERY_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {EXECUTION_RECOVERY_SCHEMA_VERSION}")
        if self.decision_id:
            object.__setattr__(self, "decision_id", bounded_text(self.decision_id, "decision_id", maximum=200))
        if not self.trace_hash:
            object.__setattr__(self, "trace_hash", hashlib.sha256(canonical_json(self._identity()).encode("utf-8")).hexdigest())
        else:
            object.__setattr__(self, "trace_hash", validate_sha256(self.trace_hash, "trace_hash"))

    def _identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id, "case_id": self.case_id, "stage": self.stage,
            "action": self.action, "belief_id": self.belief.belief_id,
            "options": [item.to_dict() for item in self.options],
            "baseline_cps": dict(sorted(self.baseline_cps.items())),
            "recovery_advantage_usd": self.recovery_advantage_usd,
            "decision_confidence": self.decision_confidence,
            "trigger_reasons": list(self.trigger_reasons), "reason_codes": list(self.reason_codes),
            "evidence_ids": list(self.evidence_ids), "policy_revision": self.policy_revision,
            "schema_version": self.schema_version,
        }

    @property
    def computed_id(self) -> str:
        return "recovery_decision_" + stable_digest(self._identity(), length=32)

    def to_dict(self) -> dict[str, Any]:
        result = {
            **self._identity(), "belief": self.belief.to_dict(), "advisory_only": self.advisory_only,
            "production_route_intervention": self.production_route_intervention, "provider_calls": self.provider_calls,
            "network_calls": self.network_calls, "decision_id": self.decision_id or self.computed_id,
            "trace_hash": self.trace_hash,
        }
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ExecutionRecoveryDecisionV1":
        validate_exact_fields(
            value,
            required=frozenset({
                "project_id", "case_id", "stage", "action", "belief", "options", "baseline_cps",
                "recovery_advantage_usd", "decision_confidence", "trigger_reasons", "reason_codes",
                "evidence_ids", "advisory_only", "production_route_intervention", "provider_calls",
                "network_calls", "policy_revision", "schema_version",
            }),
            optional=frozenset({"decision_id", "trace_hash", "belief_id"}),
        )
        item = cls(
            project_id=value["project_id"], case_id=value["case_id"], stage=value["stage"], action=value["action"],
            belief=ProgressBeliefV1.from_dict(value["belief"]),
            options=tuple(RecoveryOptionV1.from_dict(option) for option in value["options"]),
            baseline_cps=value["baseline_cps"], recovery_advantage_usd=value["recovery_advantage_usd"],
            decision_confidence=value["decision_confidence"], trigger_reasons=tuple(value["trigger_reasons"]),
            reason_codes=tuple(value["reason_codes"]), evidence_ids=tuple(value["evidence_ids"]),
            advisory_only=value["advisory_only"], production_route_intervention=value["production_route_intervention"],
            provider_calls=value["provider_calls"], network_calls=value["network_calls"],
            decision_id=value.get("decision_id", ""), trace_hash=value.get("trace_hash", ""),
            policy_revision=value["policy_revision"], schema_version=value["schema_version"],
        )
        if "belief_id" in value and value["belief_id"] != item.belief.belief_id:
            raise ValueError("belief_id does not match content")
        if item.decision_id and item.decision_id != value["decision_id"]:
            raise ValueError("decision_id mismatch")
        return item


class ExecutionRecoveryPlugin(Protocol):
    """Optional deterministic alternative planner used only for offline ablations."""

    def decide(self, snapshot: ExecutionSnapshotV1) -> ExecutionRecoveryDecisionV1: ...


class ExecutionBeliefUpdater:
    """Convert a frozen execution snapshot into a conservative progress belief."""

    @staticmethod
    def _descendants(nodes: tuple[ExecutionNodeStateV1, ...], root: str) -> set[str]:
        children: dict[str, list[str]] = {item.node_id: [] for item in nodes}
        for item in nodes:
            for dependency in item.dependencies:
                if item.dependency_types.get(dependency, "hard") in {"hard", "validation"}:
                    children[dependency].append(item.node_id)
        result: set[str] = {root}
        queue = [root]
        while queue:
            current = queue.pop(0)
            for child in children.get(current, []):
                if child not in result:
                    result.add(child)
                    queue.append(child)
        return result

    @staticmethod
    def _critical_path(nodes: tuple[ExecutionNodeStateV1, ...]) -> int:
        remaining = {item.node_id for item in nodes if item.status not in {"validated", "skipped"}}
        durations = {item.node_id: int(item.predicted_latency_ms.p95) for item in nodes}
        dependencies = {item.node_id: tuple(dep for dep in item.dependencies if dep in remaining) for item in nodes}
        memo: dict[str, int] = {}

        def longest(node_id: str, stack: set[str]) -> int:
            if node_id in memo:
                return memo[node_id]
            if node_id in stack:
                raise ValueError("execution DAG contains a cycle")
            stack.add(node_id)
            value = durations[node_id] + max((longest(child, stack) for child in remaining if node_id in dependencies[child]), default=0)
            stack.remove(node_id)
            memo[node_id] = value
            return value

        return max((longest(node_id, set()) for node_id in remaining), default=0)

    def update(self, snapshot: ExecutionSnapshotV1) -> ProgressBeliefV1:
        nodes = snapshot.nodes
        probabilities: dict[str, float] = {}
        lower = 1.0
        mean = 1.0
        upper = 1.0
        validated = rejected = active = 0
        for item in nodes:
            prior = item.predicted_success
            if item.status == "validated":
                validated += 1
                score = item.validation_score if item.validation_score is not None else min(0.98, max(0.85, prior.mean))
                item_mean, item_lower, item_upper = score, max(0.70, score - 0.08), min(1.0, score + 0.02)
            elif item.status == "rejected":
                rejected += 1
                item_mean, item_lower, item_upper = min(0.25, prior.mean * 0.25), 0.02, min(0.45, prior.upper * 0.5)
            elif item.status == "suspect":
                active += 1
                item_mean, item_lower, item_upper = prior.mean * 0.55, prior.lower * 0.35, prior.upper * 0.75
            elif item.status == "blocked":
                active += 1
                item_mean, item_lower, item_upper = prior.mean * 0.35, prior.lower * 0.20, prior.upper * 0.55
            else:
                active += int(item.status == "running")
                item_mean, item_lower, item_upper = prior.mean, prior.lower, prior.upper
            probabilities[item.node_id] = round(max(0.0, min(1.0, item_mean)), 6)
            mean *= item_mean
            lower *= item_lower
            upper *= item_upper
        remaining = len(nodes) - validated - sum(item.status == "skipped" for item in nodes)
        all_artifacts = set(snapshot.validated_artifact_ids)
        reusable = set(snapshot.reusable_artifact_ids)
        for item in nodes:
            all_artifacts.update(item.artifact_refs)
            all_artifacts.update(item.accepted_artifact_refs)
            reusable.update(item.reusable_artifact_refs)
        accepted = len(set(snapshot.validated_artifact_ids) | {ref for item in nodes for ref in item.accepted_artifact_refs})
        polluted = set(snapshot.polluted_node_ids)
        if snapshot.failure_node_id:
            polluted.update(self._descendants(nodes, snapshot.failure_node_id))
        resource = snapshot.resource
        pressure = min(1.0, resource.queue_depth / max(1.0, resource.queue_depth + resource.available_slots + 1.0))
        if resource.available_slots == 0:
            pressure = min(1.0, pressure + 0.35)
        pressure = min(1.0, pressure + max(0.0, resource.price_multiplier - 1.0) * 0.25)
        families = {item.validator_family for item in nodes if item.status not in {"skipped"}}
        reasons = set(snapshot.trigger_kinds)
        if snapshot.failure_node_id:
            reasons.add("failure")
        if snapshot.required_info_missing:
            reasons.add("missing_information")
        if snapshot.deadline_remaining_ms < self._critical_path(nodes):
            reasons.add("deadline_change")
        if snapshot.budget_remaining_usd < sum(item.predicted_cost_usd.mean for item in nodes if item.status not in {"validated", "skipped"}):
            reasons.add("budget_drift")
        codes = {
            "progress_distribution_updated",
            "critical_path_recomputed",
            "sunk_cost_excluded",
            "artifact_reuse_considered",
            "pollution_scope_propagated",
            "resource_pressure_included",
            "validator_correlation_included",
        }
        reuse_savings = min(sum(item.predicted_cost_usd.mean for item in nodes), len(reusable) * 0.01)
        return ProgressBeliefV1(
            project_id=snapshot.project_id,
            case_id=snapshot.case_id,
            plan_revision=snapshot.plan_revision,
            stage=snapshot.stage,
            success_probability=ProbabilityInterval(mean=max(0.0, min(1.0, mean)), lower=max(0.0, min(1.0, lower)), upper=max(0.0, min(1.0, upper))),
            node_success_probabilities=probabilities,
            validated_nodes=validated,
            rejected_nodes=rejected,
            active_nodes=active,
            remaining_nodes=max(0, remaining),
            critical_path_remaining_ms=self._critical_path(nodes),
            remaining_budget_usd=snapshot.budget_remaining_usd,
            deadline_remaining_ms=snapshot.deadline_remaining_ms,
            artifact_acceptance={"accepted": accepted, "total": len(all_artifacts), "reusable": len(reusable), "polluted": len(polluted)},
            pollution_scope=tuple(sorted(polluted)),
            resource_pressure=round(pressure, 6),
            validator_diversity=round(len(families) / max(1, len(nodes)), 6),
            sunk_cost_excluded_usd=snapshot.spent_cost_usd,
            reusable_artifact_savings_usd=round(reuse_savings, 6),
            trigger_reasons=tuple(sorted(reasons)),
            reason_codes=tuple(sorted(codes)),
            evidence_ids=snapshot.evidence_ids,
            observed_at=snapshot.observed_at,
        )


class EconomicRecoveryPlanner:
    """Compare bounded recovery actions using incremental economics only."""

    def __init__(self, updater: ExecutionBeliefUpdater | None = None) -> None:
        self.updater = updater or ExecutionBeliefUpdater()

    @staticmethod
    def _descendants(nodes: tuple[ExecutionNodeStateV1, ...], root: str) -> set[str]:
        return ExecutionBeliefUpdater._descendants(nodes, root)

    def _options(self, snapshot: ExecutionSnapshotV1, belief: ProgressBeliefV1) -> tuple[RecoveryOptionV1, ...]:
        nodes = snapshot.nodes
        by_id = {item.node_id: item for item in nodes}
        remaining = tuple(item.node_id for item in nodes if item.status not in {"validated", "skipped"})
        failed = by_id.get(snapshot.failure_node_id or "")
        polluted = set(belief.pollution_scope)
        base_cost = sum(by_id[node_id].predicted_cost_usd.mean for node_id in remaining)
        base_p95_cost = sum(by_id[node_id].predicted_cost_usd.p95 for node_id in remaining)
        reuse = belief.reusable_artifact_savings_usd
        risk_loss = max(0.1, snapshot.risk_loss_usd)
        failure_active = failed is not None and failed.status in {"rejected", "suspect", "blocked"}
        resource_pressure = belief.resource_pressure
        deadline_pressure = belief.critical_path_remaining_ms > snapshot.deadline_remaining_ms
        missing_info = bool(snapshot.required_info_missing)
        if failure_active:
            baseline_success = max(0.03, belief.success_probability.mean * 0.66)
        else:
            baseline_success = belief.success_probability.mean

        def make(action: str, *, target: tuple[str, ...], raw_cost: float, raw_p95: float, latency: int, success: float, switching: float = 0.0, validator: str = "existing", extra: tuple[str, ...] = (), required: tuple[str, ...] = ()) -> RecoveryOptionV1:
            savings = min(reuse, raw_cost * 0.6) if action not in {"REQUEST_MISSING_INFO", "STOP_AND_RETURN_PARTIAL"} else 0.0
            cost = max(0.0, raw_cost + switching - savings)
            p95_cost = max(cost, raw_p95 + switching - savings)
            success = max(0.01, min(0.99, success))
            checks = {
                "budget_remaining": cost <= snapshot.budget_remaining_usd + 1e-9,
                "deadline_remaining": latency <= snapshot.deadline_remaining_ms,
                "provider_available": snapshot.resource.provider_available or action in {"STOP_AND_RETURN_PARTIAL", "REQUEST_MISSING_INFO"},
                "required_capacity": snapshot.resource.available_slots > 0 or action in {"STOP_AND_RETURN_PARTIAL", "REQUEST_MISSING_INFO"},
                "attempt_limit": not required or all(by_id[node_id].attempts < by_id[node_id].max_attempts for node_id in required),
                "retry_budget": action != "RETRY_NODE" or snapshot.max_retries > 0,
                "switch_budget": action != "SWITCH_MODEL" or snapshot.max_switches > 0,
                "subgraph_budget": action != "REPLAN_SUBGRAPH" or snapshot.max_subgraph_replans > 0,
                "full_replan_budget": action != "FULL_REPLAN" or snapshot.max_full_replans > 0,
                "alternate_route": action != "SWITCH_MODEL" or snapshot.resource.alternate_route_available,
                "orthogonal_validator": action != "SWITCH_MODEL" or snapshot.resource.orthogonal_validator_available,
            }
            expected_loss = (1.0 - success) * risk_loss + cost + latency / 100_000.0
            cps = (cost + (1.0 - success) * risk_loss) / success
            reasons = set(extra)
            if savings > 0:
                reasons.add("validated_artifact_reused")
            reasons.add("sunk_cost_excluded")
            if action == "SWITCH_MODEL":
                reasons.add("orthogonal_validator_required")
            feasible = all(checks.values())
            return RecoveryOptionV1(
                action=action, feasible=feasible, target_node_ids=target, validator_family=validator,
                expected_success=ProbabilityInterval(success, max(0.0, success - 0.12), min(1.0, success + 0.06)),
                incremental_cost_usd=NonNegativeEstimate(cost, p95_cost),
                expected_latency_ms=NonNegativeEstimate(latency, max(latency, int(latency * 1.35))),
                switching_cost_usd=switching, artifact_reuse_savings_usd=savings,
                sunk_cost_excluded_usd=snapshot.spent_cost_usd, expected_loss_usd=expected_loss, cps=cps,
                hard_checks=checks, reason_codes=tuple(sorted(reasons)),
            )

        options: list[RecoveryOptionV1] = []
        continue_feasible = not failure_active or not polluted
        options.append(make("CONTINUE", target=remaining, raw_cost=base_cost, raw_p95=base_p95_cost, latency=max(1, belief.critical_path_remaining_ms), success=baseline_success, extra=("no_unresolved_hard_failure" if continue_feasible else "hard_failure_requires_recovery",)))
        if failed is not None:
            options.append(make("RETRY_NODE", target=(failed.node_id,), raw_cost=failed.predicted_cost_usd.mean * 0.85 + sum(by_id[item].predicted_cost_usd.mean for item in polluted if item != failed.node_id) * 0.5, raw_p95=failed.predicted_cost_usd.p95 + sum(by_id[item].predicted_cost_usd.p95 for item in polluted if item != failed.node_id) * 0.5, latency=max(1, failed.predicted_latency_ms.p95 + 100), success=min(0.96, baseline_success + 0.13), validator=failed.validator_family, required=(failed.node_id,), extra=("node_failure_detected",)))
            options.append(make("SWITCH_MODEL", target=(failed.node_id,), raw_cost=failed.predicted_cost_usd.mean * 0.72 + sum(by_id[item].predicted_cost_usd.mean for item in polluted if item != failed.node_id) * 0.35, raw_p95=failed.predicted_cost_usd.p95 * 0.8 + sum(by_id[item].predicted_cost_usd.p95 for item in polluted if item != failed.node_id) * 0.35, latency=max(1, failed.predicted_latency_ms.p95 + 180), success=min(0.98, baseline_success + 0.23), switching=0.012, validator="orthogonal", extra=("node_failure_detected", "correlated_failure_avoided"), required=(failed.node_id,)))
        options.append(make("REQUEST_MISSING_INFO", target=remaining, raw_cost=0.003, raw_p95=0.004, latency=250, success=min(0.97, baseline_success + 0.11), validator="human-or-user", extra=("missing_information" if missing_info else "uncertainty_probe",)))
        options.append(make("REPLAN_SUBGRAPH", target=tuple(sorted(polluted or set(remaining))), raw_cost=max(0.01, base_cost * 0.58), raw_p95=max(0.015, base_p95_cost * 0.62), latency=max(1, int(belief.critical_path_remaining_ms * 0.75) + 120), success=min(0.98, baseline_success + 0.18), switching=0.008, validator="orthogonal", extra=("pollution_scoped", "validated_artifact_reused")))
        options.append(make("FULL_REPLAN", target=remaining, raw_cost=max(0.02, base_cost * 0.86), raw_p95=max(0.03, base_p95_cost * 0.92), latency=max(1, int(belief.critical_path_remaining_ms * 0.95) + 300), success=min(0.99, baseline_success + 0.21), switching=0.03, validator="independent", extra=("global_reassessment",)))
        # A partial result is not equivalent to a completed goal: discount the
        # validated-node fraction by the unresolved critical-path share.
        partial_success = (belief.validated_nodes / max(1, len(nodes))) * 0.75
        options.append(make("STOP_AND_RETURN_PARTIAL", target=tuple(sorted(set(item.node_id for item in nodes if item.status == "validated"))), raw_cost=0.0, raw_p95=0.0, latency=0, success=max(0.05, partial_success), validator="independent", extra=("fail_closed_available",)))
        # Resource and deadline shocks make waiting with CONTINUE unsafe even if
        # its static score looks cheaper.  Keep the option for audit but mark it
        # infeasible through a hard check by rebuilding its evidence.
        if resource_pressure > 0.75 or deadline_pressure:
            updated: list[RecoveryOptionV1] = []
            for option in options:
                if option.action == "CONTINUE":
                    checks = dict(option.hard_checks)
                    checks["dynamic_resource_or_deadline"] = False
                    option = RecoveryOptionV1.from_dict({
                        **option.to_dict(),
                        "feasible": False,
                        "hard_checks": checks,
                        "reason_codes": list(option.reason_codes) + ["dynamic_trigger_requires_redecision"],
                    })
                updated.append(option)
            options = updated
        return tuple(options)

    def decide(self, snapshot: ExecutionSnapshotV1) -> tuple[ExecutionRecoveryDecisionV1, ProgressBeliefV1]:
        belief = self.updater.update(snapshot)
        options = self._options(snapshot, belief)
        feasible = [item for item in options if item.feasible]
        selected = min(feasible or [item for item in options if item.action == "STOP_AND_RETURN_PARTIAL"], key=lambda item: (item.expected_loss_usd, item.action))
        by_action = {item.action: item for item in options}
        retry_cps = by_action["RETRY_NODE"].cps
        full_cps = by_action["FULL_REPLAN"].cps
        baseline_cps = {"fixed_retry": retry_cps, "fixed_full_replan": full_cps, "dynamic_selected": selected.cps}
        advantage = max(0.0, min(retry_cps, full_cps) - selected.cps)
        feasible_losses = sorted(item.expected_loss_usd for item in feasible)
        margin = (feasible_losses[1] - feasible_losses[0]) if len(feasible_losses) > 1 else 0.0
        confidence = min(0.98, max(0.35, 0.55 + min(0.25, margin) + (0.08 if belief.validator_diversity >= 0.5 else 0.0) - (0.12 if belief.resource_pressure > 0.8 else 0.0)))
        reasons = set(belief.reason_codes) | set(selected.reason_codes)
        if advantage > 0:
            reasons.add("cps_better_than_fixed_baselines")
        if not feasible:
            reasons.add("fail_closed_no_feasible_recovery")
        decision = ExecutionRecoveryDecisionV1(
            project_id=snapshot.project_id, case_id=snapshot.case_id, stage=snapshot.stage,
            action=selected.action, belief=belief, options=options, baseline_cps=baseline_cps,
            recovery_advantage_usd=round(advantage, 8), decision_confidence=round(confidence, 6),
            trigger_reasons=belief.trigger_reasons, reason_codes=tuple(sorted(reasons)),
            evidence_ids=snapshot.evidence_ids,
        )
        return decision, belief


def build_demo_snapshot(*, project_id: str = "local-default", case_id: str = "offline-recovery-demo") -> ExecutionSnapshotV1:
    """Build a deterministic fault-injection fixture for CLI/UI and tests."""

    def success(mean: float, lower: float, upper: float) -> ProbabilityInterval:
        return ProbabilityInterval(mean, lower, upper)

    def node(node_id: str, dependencies: tuple[str, ...], status: str, mean: float, cost: float, latency: int, *, validator: str, failure: str | None = None, accepted: tuple[str, ...] = (), reusable: tuple[str, ...] = (), alternate: bool = False, score: float | None = None) -> ExecutionNodeStateV1:
        return ExecutionNodeStateV1(
            node_id=node_id, dependencies=dependencies, dependency_types={dep: "hard" for dep in dependencies}, status=status,
            predicted_success=success(mean, max(0.05, mean - 0.12), min(0.99, mean + 0.08)),
            predicted_cost_usd=NonNegativeEstimate(cost, cost * 1.5), predicted_latency_ms=NonNegativeEstimate(latency, int(latency * 1.4)),
            actual_cost_usd=cost if status == "validated" else 0.0, actual_latency_ms=latency if status == "validated" else 0,
            attempts=1 if status in {"validated", "rejected"} else 0, max_attempts=2,
            artifact_refs=accepted, accepted_artifact_refs=accepted, reusable_artifact_refs=reusable,
            failure_class=failure, validation_score=score, validator_family=validator,
            alternate_executor_available=alternate, criticality=1.0,
        )

    nodes = (
        node("research", (), "validated", 0.95, 0.04, 350, validator="deterministic", accepted=("artifact-research",), reusable=("artifact-research",), score=0.96),
        node("draft", ("research",), "rejected", 0.72, 0.08, 600, validator="same-judge", failure="validator_conflict", alternate=True),
        node("polish", ("draft",), "pending", 0.86, 0.07, 500, validator="orthogonal-judge"),
        node("final", ("polish",), "pending", 0.91, 0.03, 250, validator="deterministic"),
    )
    return ExecutionSnapshotV1(
        project_id=project_id, case_id=case_id, objective_hash="a" * 64, plan_revision=3, stage="milestone", nodes=nodes,
        budget_remaining_usd=0.30, deadline_remaining_ms=3000, spent_cost_usd=0.04, spent_latency_ms=350,
        validated_artifact_ids=("artifact-research",), reusable_artifact_ids=("artifact-research",), polluted_node_ids=(),
        failure_node_id="draft", failure_class="validator_conflict", failure_confidence=0.91,
        required_info_missing=(), trigger_kinds=("failure", "milestone"),
        resource=ExecutionResourceBeliefV1(queue_depth=1, available_slots=2, price_multiplier=1.0, provider_available=True, telemetry_age_ms=100, resource_revision="fixture-r1", alternate_route_available=True, orthogonal_validator_available=True),
        risk_loss_usd=1.5, max_retries=2, max_switches=1, max_subgraph_replans=1, max_full_replans=1,
        observed_at="2026-08-10T00:00:00Z", evidence_ids=("execution-recovery-offline-fixture",),
    )


def run_demo() -> ExecutionRecoveryDecisionV1:
    return EconomicRecoveryPlanner().decide(build_demo_snapshot())[0]


__all__ = [
    "DEPENDENCY_TYPES", "EXECUTION_ACTIONS", "EXECUTION_RECOVERY_POLICY_REVISION", "EXECUTION_RECOVERY_SCHEMA_VERSION",
    "EXECUTION_STAGES", "ExecutionBeliefUpdater", "ExecutionNodeStateV1", "ExecutionRecoveryDecisionV1",
    "ExecutionRecoveryPlugin", "ExecutionResourceBeliefV1", "ExecutionSnapshotV1", "EconomicRecoveryPlanner",
    "NODE_STATUSES", "ProgressBeliefV1", "RecoveryOptionV1", "TRIGGER_KINDS", "build_demo_snapshot", "run_demo",
]
