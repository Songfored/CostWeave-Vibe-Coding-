"""Evidence graph and calibrated routing contracts for v0.6.2.

This module deliberately contains a small, deterministic reference
implementation.  It does not call a model, provider, network or external
statistics package.  The graph stores *evidence about conditions* rather than
claiming that a model has a fixed capability score.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .common import (
    NonNegativeEstimate,
    ProbabilityInterval,
    bounded_text,
    canonical_json,
    finite_number,
    probability,
    stable_digest,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_sha256,
    validate_timestamp,
)


CAPABILITY_GRAPH_SCHEMA_VERSION = "v0.6.2"
CAPABILITY_GRAPH_POLICY_REVISION = "capability-evidence-graph-router-v1"
CALIBRATION_REVISION = "decayed-beta-conformal-v1"

CAPABILITY_NODE_TYPES = (
    "TaskFamily",
    "ModelRevision",
    "ReasoningTier",
    "Tool",
    "Modality",
    "ProviderDeployment",
    "Validator",
)
EVIDENCE_SOURCE_KINDS = (
    "public_benchmark",
    "independent_outcome",
    "hard_acceptance",
    "human_blind_review",
    "controlled_experiment",
)
ROUTER_ACTIONS = ("ROUTE", "ABSTAIN", "ESCALATE")
CALIBRATION_STATUSES = ("prior", "calibrated", "stale", "ood", "insufficient")
RISK_LEVELS = ("low", "medium", "high", "critical")


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be a non-negative int")
    if value < 0:
        raise ValueError(f"{name} must be a non-negative int")
    return value


def _non_negative_float(value: Any, name: str) -> float:
    return finite_number(value, name, minimum=0.0)


def _iso_datetime(value: str) -> datetime:
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def _age_days(observed_at: str, now: str) -> float:
    age = (_iso_datetime(now) - _iso_datetime(observed_at)).total_seconds() / 86400.0
    if age < -1e-9:
        raise ValueError("observed_at cannot be after now")
    return max(0.0, age)


def capability_condition_key(
    *,
    task_family: str,
    model_revision: str,
    reasoning_tier: str,
    provider_deployment: str,
    tool_ids: Sequence[str] = (),
    modality_ids: Sequence[str] = (),
    validator_id: str = "validator:none",
) -> str:
    """Return the canonical graph edge key for one set of conditions."""

    parts = {
        "task_family": bounded_text(task_family, "task_family", maximum=64),
        "model_revision": bounded_text(model_revision, "model_revision"),
        "reasoning_tier": bounded_text(reasoning_tier, "reasoning_tier"),
        "provider_deployment": bounded_text(provider_deployment, "provider_deployment"),
        "tool_ids": list(unique_texts(tool_ids, "tool_ids", maximum_items=32)),
        "modality_ids": list(unique_texts(modality_ids, "modality_ids", maximum_items=16)),
        "validator_id": bounded_text(validator_id, "validator_id"),
    }
    return stable_digest(parts, length=64)


@dataclass(frozen=True, slots=True)
class CapabilityNodeV1:
    node_id: str
    node_type: str
    label: str
    revision: str = "v1"
    attributes: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_id", bounded_text(self.node_id, "node_id"))
        if self.node_type not in CAPABILITY_NODE_TYPES:
            raise ValueError(f"unsupported node_type: {self.node_type!r}")
        object.__setattr__(self, "label", bounded_text(self.label, "label"))
        object.__setattr__(self, "revision", bounded_text(self.revision, "revision", maximum=64))
        if not isinstance(self.attributes, Mapping):
            raise TypeError("attributes must be a mapping")
        # canonical_json rejects NaN, non-string keys and unsupported values.
        canonical_json(dict(self.attributes))
        object.__setattr__(self, "attributes", dict(self.attributes))

    @property
    def node_hash(self) -> str:
        return stable_digest(self.to_dict(include_hash=False), length=64)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = {
            "node_id": self.node_id,
            "node_type": self.node_type,
            "label": self.label,
            "revision": self.revision,
            "attributes": dict(self.attributes),
            "schema_version": CAPABILITY_GRAPH_SCHEMA_VERSION,
        }
        if include_hash:
            result["node_hash"] = self.node_hash
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityNodeV1":
        validate_exact_fields(
            value,
            required=frozenset({"node_id", "node_type", "label", "revision", "attributes", "schema_version"}),
            optional=frozenset({"node_hash"}),
        )
        if value["schema_version"] != CAPABILITY_GRAPH_SCHEMA_VERSION:
            raise ValueError("unsupported capability graph schema_version")
        item = cls(
            node_id=value["node_id"],
            node_type=value["node_type"],
            label=value["label"],
            revision=value["revision"],
            attributes=value["attributes"],
        )
        if "node_hash" in value and value["node_hash"] != item.node_hash:
            raise ValueError("node_hash does not match node content")
        return item


@dataclass(frozen=True, slots=True)
class CapabilityEvidenceV1:
    """One immutable, condition-specific evidence record.

    The record intentionally contains aggregates and hashes, not prompts or
    model outputs.  Counts are integers; a source with a weighted sample must
    be transformed by the importer and retain its original source digest.
    """

    evidence_id: str
    project_id: str
    task_family: str
    model_revision: str
    reasoning_tier: str
    provider_deployment: str
    source_kind: str
    source_id: str
    source_version: str
    selection_mechanism: str
    evidence_level: str
    failure_correlation_group: str
    successes: int
    failures: int
    retry_events: int
    escalation_events: int
    rework_events: int
    correlated_failure_events: int
    cost_usd: NonNegativeEstimate
    latency_ms: NonNegativeEstimate
    observed_at: str
    source_digest: str
    tool_ids: tuple[str, ...] = ()
    modality_ids: tuple[str, ...] = ()
    validator_id: str = "validator:none"
    expires_at: str | None = None
    schema_version: str = CAPABILITY_GRAPH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "evidence_id",
            "project_id",
            "model_revision",
            "reasoning_tier",
            "provider_deployment",
            "source_id",
            "source_version",
            "selection_mechanism",
            "failure_correlation_group",
            "validator_id",
        ):
            object.__setattr__(self, name, bounded_text(getattr(self, name), name))
        task_family = bounded_text(self.task_family, "task_family", maximum=64)
        if any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for char in task_family):
            raise ValueError("task_family must use lowercase slug characters")
        object.__setattr__(self, "task_family", task_family)
        if self.source_kind not in EVIDENCE_SOURCE_KINDS:
            raise ValueError(f"unsupported source_kind: {self.source_kind!r}")
        if self.evidence_level not in {"E0-simulated", "E1-public-replay", "E2-directional-real", "E3-isolated-real", "E4-formal-benchmark", "E5-shadow-canary"}:
            raise ValueError(f"unsupported evidence_level: {self.evidence_level!r}")
        for name in (
            "successes",
            "failures",
            "retry_events",
            "escalation_events",
            "rework_events",
            "correlated_failure_events",
        ):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name))
        if self.successes + self.failures <= 0:
            raise ValueError("evidence must contain at least one success or failure")
        sample_count = self.successes + self.failures
        for name in ("retry_events", "escalation_events", "rework_events", "correlated_failure_events"):
            if getattr(self, name) > sample_count:
                raise ValueError(f"{name} cannot exceed sample count")
        if not isinstance(self.cost_usd, NonNegativeEstimate):
            raise TypeError("cost_usd must be NonNegativeEstimate")
        if not isinstance(self.latency_ms, NonNegativeEstimate):
            raise TypeError("latency_ms must be NonNegativeEstimate")
        object.__setattr__(self, "tool_ids", unique_texts(self.tool_ids, "tool_ids", maximum_items=32))
        object.__setattr__(self, "modality_ids", unique_texts(self.modality_ids, "modality_ids", maximum_items=16))
        object.__setattr__(self, "observed_at", validate_timestamp(self.observed_at, "observed_at"))
        if self.expires_at is not None:
            object.__setattr__(self, "expires_at", validate_timestamp(self.expires_at, "expires_at"))
            if _iso_datetime(self.expires_at) < _iso_datetime(self.observed_at):
                raise ValueError("expires_at cannot precede observed_at")
        object.__setattr__(self, "source_digest", validate_sha256(self.source_digest, "source_digest"))
        if self.schema_version != CAPABILITY_GRAPH_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {CAPABILITY_GRAPH_SCHEMA_VERSION}")

    @property
    def sample_count(self) -> int:
        return self.successes + self.failures

    @property
    def condition_key(self) -> str:
        return capability_condition_key(
            task_family=self.task_family,
            model_revision=self.model_revision,
            reasoning_tier=self.reasoning_tier,
            provider_deployment=self.provider_deployment,
            tool_ids=self.tool_ids,
            modality_ids=self.modality_ids,
            validator_id=self.validator_id,
        )

    def to_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        result = {
            "project_id": self.project_id,
            "task_family": self.task_family,
            "model_revision": self.model_revision,
            "reasoning_tier": self.reasoning_tier,
            "provider_deployment": self.provider_deployment,
            "tool_ids": list(self.tool_ids),
            "modality_ids": list(self.modality_ids),
            "validator_id": self.validator_id,
            "source_kind": self.source_kind,
            "source_id": self.source_id,
            "source_version": self.source_version,
            "selection_mechanism": self.selection_mechanism,
            "evidence_level": self.evidence_level,
            "failure_correlation_group": self.failure_correlation_group,
            "successes": self.successes,
            "failures": self.failures,
            "retry_events": self.retry_events,
            "escalation_events": self.escalation_events,
            "rework_events": self.rework_events,
            "correlated_failure_events": self.correlated_failure_events,
            "sample_count": self.sample_count,
            "cost_usd": self.cost_usd.to_dict(),
            "latency_ms": self.latency_ms.to_dict(),
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "source_digest": self.source_digest,
            "schema_version": self.schema_version,
        }
        if include_id:
            result["evidence_id"] = self.evidence_id
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityEvidenceV1":
        required = frozenset({
            "evidence_id", "project_id", "task_family", "model_revision", "reasoning_tier",
            "provider_deployment", "tool_ids", "modality_ids", "validator_id", "source_kind",
            "source_id", "source_version", "selection_mechanism", "evidence_level",
            "failure_correlation_group", "successes", "failures", "retry_events", "escalation_events",
            "rework_events", "correlated_failure_events", "cost_usd", "latency_ms", "observed_at",
            "expires_at", "source_digest", "schema_version",
        })
        validate_exact_fields(value, required=required, optional=frozenset({"sample_count", "condition_key"}))
        if value["schema_version"] != CAPABILITY_GRAPH_SCHEMA_VERSION:
            raise ValueError("unsupported capability evidence schema_version")
        item = cls(
            evidence_id=value["evidence_id"],
            project_id=value["project_id"],
            task_family=value["task_family"],
            model_revision=value["model_revision"],
            reasoning_tier=value["reasoning_tier"],
            provider_deployment=value["provider_deployment"],
            tool_ids=tuple(value["tool_ids"]),
            modality_ids=tuple(value["modality_ids"]),
            validator_id=value["validator_id"],
            source_kind=value["source_kind"],
            source_id=value["source_id"],
            source_version=value["source_version"],
            selection_mechanism=value["selection_mechanism"],
            evidence_level=value["evidence_level"],
            failure_correlation_group=value["failure_correlation_group"],
            successes=value["successes"],
            failures=value["failures"],
            retry_events=value["retry_events"],
            escalation_events=value["escalation_events"],
            rework_events=value["rework_events"],
            correlated_failure_events=value["correlated_failure_events"],
            cost_usd=NonNegativeEstimate.from_dict(value["cost_usd"]),
            latency_ms=NonNegativeEstimate.from_dict(value["latency_ms"]),
            observed_at=value["observed_at"],
            expires_at=value["expires_at"],
            source_digest=value["source_digest"],
            schema_version=value["schema_version"],
        )
        if "sample_count" in value and value["sample_count"] != item.sample_count:
            raise ValueError("sample_count does not match success and failure counts")
        if "condition_key" in value and value["condition_key"] != item.condition_key:
            raise ValueError("condition_key does not match evidence conditions")
        return item


@dataclass(frozen=True, slots=True)
class CapabilityPriorV1:
    success: ProbabilityInterval = ProbabilityInterval(mean=0.5, lower=0.05, upper=0.95)
    cost_usd: NonNegativeEstimate = NonNegativeEstimate(mean=0.0, p95=0.0)
    latency_ms: NonNegativeEstimate = NonNegativeEstimate(mean=0.0, p95=0.0)
    source: str = "wide-global-prior"
    strength: float = 2.0
    cost_known: bool = False
    latency_known: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.success, ProbabilityInterval):
            raise TypeError("success must be ProbabilityInterval")
        if not isinstance(self.cost_usd, NonNegativeEstimate) or not isinstance(self.latency_ms, NonNegativeEstimate):
            raise TypeError("cost_usd and latency_ms must be NonNegativeEstimate")
        object.__setattr__(self, "source", bounded_text(self.source, "source", maximum=100))
        object.__setattr__(self, "strength", _non_negative_float(self.strength, "strength"))
        if not isinstance(self.cost_known, bool) or not isinstance(self.latency_known, bool):
            raise TypeError("cost_known and latency_known must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {"success": self.success.to_dict(), "cost_usd": self.cost_usd.to_dict(), "latency_ms": self.latency_ms.to_dict(), "source": self.source, "strength": self.strength, "cost_known": self.cost_known, "latency_known": self.latency_known}


@dataclass(frozen=True, slots=True)
class CapabilityPosteriorV2:
    project_id: str
    task_family: str
    model_revision: str
    reasoning_tier: str
    provider_deployment: str
    validator_id: str
    success: ProbabilityInterval
    cost_usd: NonNegativeEstimate
    latency_ms: NonNegativeEstimate
    retry_probability: ProbabilityInterval
    escalation_probability: ProbabilityInterval
    rework_probability: ProbabilityInterval
    failure_correlation: ProbabilityInterval
    sample_count: int
    effective_sample_size: float
    evidence_level: str
    calibration_status: str
    coverage: float
    stale: bool
    ood: bool
    prior_source: str
    source_ids: tuple[str, ...]
    as_of: str
    tool_ids: tuple[str, ...] = ()
    modality_ids: tuple[str, ...] = ()
    calibration_set_id: str = "unavailable"
    schema_version: str = CAPABILITY_GRAPH_SCHEMA_VERSION
    cost_known: bool = False
    latency_known: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", bounded_text(self.project_id, "project_id"))
        object.__setattr__(self, "task_family", bounded_text(self.task_family, "task_family", maximum=64))
        for name in ("model_revision", "reasoning_tier", "provider_deployment", "validator_id", "prior_source", "calibration_set_id"):
            object.__setattr__(self, name, bounded_text(getattr(self, name), name))
        for name in ("success", "retry_probability", "escalation_probability", "rework_probability", "failure_correlation"):
            if not isinstance(getattr(self, name), ProbabilityInterval):
                raise TypeError(f"{name} must be ProbabilityInterval")
        for name in ("cost_usd", "latency_ms"):
            if not isinstance(getattr(self, name), NonNegativeEstimate):
                raise TypeError(f"{name} must be NonNegativeEstimate")
        object.__setattr__(self, "sample_count", _non_negative_int(self.sample_count, "sample_count"))
        object.__setattr__(self, "effective_sample_size", _non_negative_float(self.effective_sample_size, "effective_sample_size"))
        if self.evidence_level not in {"E0-simulated", "E1-public-replay", "E2-directional-real", "E3-isolated-real", "E4-formal-benchmark", "E5-shadow-canary"}:
            raise ValueError("unsupported evidence_level")
        if self.calibration_status not in CALIBRATION_STATUSES:
            raise ValueError("unsupported calibration_status")
        object.__setattr__(self, "coverage", probability(self.coverage, "coverage"))
        if not isinstance(self.stale, bool) or not isinstance(self.ood, bool):
            raise TypeError("stale and ood must be bool")
        object.__setattr__(self, "source_ids", unique_texts(self.source_ids, "source_ids", maximum_items=128))
        object.__setattr__(self, "tool_ids", unique_texts(self.tool_ids, "tool_ids", maximum_items=32))
        object.__setattr__(self, "modality_ids", unique_texts(self.modality_ids, "modality_ids", maximum_items=16))
        object.__setattr__(self, "as_of", validate_timestamp(self.as_of, "as_of"))
        if not isinstance(self.cost_known, bool) or not isinstance(self.latency_known, bool):
            raise TypeError("cost_known and latency_known must be bool")
        if self.schema_version != CAPABILITY_GRAPH_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {CAPABILITY_GRAPH_SCHEMA_VERSION}")

    @property
    def condition_key(self) -> str:
        return capability_condition_key(
            task_family=self.task_family,
            model_revision=self.model_revision,
            reasoning_tier=self.reasoning_tier,
            provider_deployment=self.provider_deployment,
            tool_ids=self.tool_ids,
            modality_ids=self.modality_ids,
            validator_id=self.validator_id,
        )

    @property
    def posterior_id(self) -> str:
        return stable_id("capability_posterior", self._identity())

    def _identity(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "task_family": self.task_family,
            "model_revision": self.model_revision,
            "reasoning_tier": self.reasoning_tier,
            "provider_deployment": self.provider_deployment,
            "validator_id": self.validator_id,
            "tool_ids": list(self.tool_ids),
            "modality_ids": list(self.modality_ids),
            "success": self.success.to_dict(),
            "cost_usd": self.cost_usd.to_dict(),
            "latency_ms": self.latency_ms.to_dict(),
            "retry_probability": self.retry_probability.to_dict(),
            "escalation_probability": self.escalation_probability.to_dict(),
            "rework_probability": self.rework_probability.to_dict(),
            "failure_correlation": self.failure_correlation.to_dict(),
            "sample_count": self.sample_count,
            "effective_sample_size": self.effective_sample_size,
            "evidence_level": self.evidence_level,
            "calibration_status": self.calibration_status,
            "coverage": self.coverage,
            "stale": self.stale,
            "ood": self.ood,
            "prior_source": self.prior_source,
            "source_ids": list(self.source_ids),
            "as_of": self.as_of,
            "calibration_set_id": self.calibration_set_id,
            "cost_known": self.cost_known,
            "latency_known": self.latency_known,
            "schema_version": self.schema_version,
        }

    def to_dict(self, *, include_id: bool = True) -> dict[str, Any]:
        result = self._identity()
        if include_id:
            result["posterior_id"] = self.posterior_id
        result["condition_key"] = self.condition_key
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CapabilityPosteriorV2":
        required = frozenset({
            "project_id", "task_family", "model_revision", "reasoning_tier", "provider_deployment",
            "validator_id", "tool_ids", "modality_ids", "success", "cost_usd", "latency_ms",
            "retry_probability", "escalation_probability", "rework_probability", "failure_correlation",
            "sample_count", "effective_sample_size", "evidence_level", "calibration_status", "coverage",
            "stale", "ood", "prior_source", "source_ids", "as_of", "calibration_set_id", "schema_version",
        })
        validate_exact_fields(value, required=required, optional=frozenset({"posterior_id", "condition_key", "cost_known", "latency_known"}))
        if value["schema_version"] != CAPABILITY_GRAPH_SCHEMA_VERSION:
            raise ValueError("unsupported capability posterior schema_version")
        item = cls(
            project_id=value["project_id"], task_family=value["task_family"], model_revision=value["model_revision"],
            reasoning_tier=value["reasoning_tier"], provider_deployment=value["provider_deployment"], validator_id=value["validator_id"],
            tool_ids=tuple(value["tool_ids"]), modality_ids=tuple(value["modality_ids"]), success=ProbabilityInterval.from_dict(value["success"]),
            cost_usd=NonNegativeEstimate.from_dict(value["cost_usd"]), latency_ms=NonNegativeEstimate.from_dict(value["latency_ms"]),
            retry_probability=ProbabilityInterval.from_dict(value["retry_probability"]), escalation_probability=ProbabilityInterval.from_dict(value["escalation_probability"]),
            rework_probability=ProbabilityInterval.from_dict(value["rework_probability"]), failure_correlation=ProbabilityInterval.from_dict(value["failure_correlation"]),
            sample_count=value["sample_count"], effective_sample_size=value["effective_sample_size"], evidence_level=value["evidence_level"],
            calibration_status=value["calibration_status"], coverage=value["coverage"], stale=value["stale"], ood=value["ood"],
            prior_source=value["prior_source"], source_ids=tuple(value["source_ids"]), as_of=value["as_of"], calibration_set_id=value["calibration_set_id"], schema_version=value["schema_version"], cost_known=value.get("cost_known", False), latency_known=value.get("latency_known", False),
        )
        if "posterior_id" in value and value["posterior_id"] != item.posterior_id:
            raise ValueError("posterior_id does not match posterior content")
        if "condition_key" in value and value["condition_key"] != item.condition_key:
            raise ValueError("condition_key does not match posterior content")
        return item


@dataclass(frozen=True, slots=True)
class RouteCandidateV1:
    route_id: str
    model_revision: str
    reasoning_tier: str
    provider_deployment: str
    validator_id: str = "validator:none"
    tool_ids: tuple[str, ...] = ()
    modality_ids: tuple[str, ...] = ()
    failure_correlation_group: str = "unknown"
    base_cost_usd: NonNegativeEstimate = NonNegativeEstimate(mean=0.0, p95=0.0)
    base_latency_ms: NonNegativeEstimate = NonNegativeEstimate(mean=0.0, p95=0.0)
    base_cost_known: bool = False
    base_latency_known: bool = False

    def __post_init__(self) -> None:
        for name in ("route_id", "model_revision", "reasoning_tier", "provider_deployment", "validator_id", "failure_correlation_group"):
            object.__setattr__(self, name, bounded_text(getattr(self, name), name))
        object.__setattr__(self, "tool_ids", unique_texts(self.tool_ids, "tool_ids", maximum_items=32))
        object.__setattr__(self, "modality_ids", unique_texts(self.modality_ids, "modality_ids", maximum_items=16))
        if not isinstance(self.base_cost_usd, NonNegativeEstimate) or not isinstance(self.base_latency_ms, NonNegativeEstimate):
            raise TypeError("base estimates must be NonNegativeEstimate")
        if not isinstance(self.base_cost_known, bool) or not isinstance(self.base_latency_known, bool):
            raise TypeError("base_cost_known and base_latency_known must be bool")

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "model_revision": self.model_revision,
            "reasoning_tier": self.reasoning_tier,
            "provider_deployment": self.provider_deployment,
            "validator_id": self.validator_id,
            "tool_ids": list(self.tool_ids),
            "modality_ids": list(self.modality_ids),
            "failure_correlation_group": self.failure_correlation_group,
            "base_cost_usd": self.base_cost_usd.to_dict(),
            "base_latency_ms": self.base_latency_ms.to_dict(),
            "base_cost_known": self.base_cost_known,
            "base_latency_known": self.base_latency_known,
            "schema_version": CAPABILITY_GRAPH_SCHEMA_VERSION,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RouteCandidateV1":
        validate_exact_fields(value, required=frozenset({"route_id", "model_revision", "reasoning_tier", "provider_deployment", "validator_id", "tool_ids", "modality_ids", "failure_correlation_group", "base_cost_usd", "base_latency_ms", "schema_version"}), optional=frozenset({"base_cost_known", "base_latency_known"}))
        if value["schema_version"] != CAPABILITY_GRAPH_SCHEMA_VERSION:
            raise ValueError("unsupported route candidate schema_version")
        return cls(
            route_id=value["route_id"], model_revision=value["model_revision"], reasoning_tier=value["reasoning_tier"], provider_deployment=value["provider_deployment"], validator_id=value["validator_id"], tool_ids=tuple(value["tool_ids"]), modality_ids=tuple(value["modality_ids"]), failure_correlation_group=value["failure_correlation_group"], base_cost_usd=NonNegativeEstimate.from_dict(value["base_cost_usd"]), base_latency_ms=NonNegativeEstimate.from_dict(value["base_latency_ms"]), base_cost_known=value.get("base_cost_known", False), base_latency_known=value.get("base_latency_known", False),
        )


@dataclass(frozen=True, slots=True)
class RouteContextV1:
    project_id: str
    task_family: str
    risk_level: str = "medium"
    ood_score: float = 0.0
    required_tools: tuple[str, ...] = ()
    required_modalities: tuple[str, ...] = ()
    deadline_remaining_ms: float = 0.0
    budget_remaining_usd: float = 0.0
    independent_validator_required: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", bounded_text(self.project_id, "project_id"))
        object.__setattr__(self, "task_family", bounded_text(self.task_family, "task_family", maximum=64))
        if self.risk_level not in RISK_LEVELS:
            raise ValueError("unsupported risk_level")
        object.__setattr__(self, "ood_score", probability(self.ood_score, "ood_score"))
        object.__setattr__(self, "required_tools", unique_texts(self.required_tools, "required_tools", maximum_items=32))
        object.__setattr__(self, "required_modalities", unique_texts(self.required_modalities, "required_modalities", maximum_items=16))
        object.__setattr__(self, "deadline_remaining_ms", _non_negative_float(self.deadline_remaining_ms, "deadline_remaining_ms"))
        object.__setattr__(self, "budget_remaining_usd", _non_negative_float(self.budget_remaining_usd, "budget_remaining_usd"))
        if not isinstance(self.independent_validator_required, bool):
            raise TypeError("independent_validator_required must be bool")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RouteContextV1":
        validate_exact_fields(value, required=frozenset({"project_id", "task_family", "risk_level", "ood_score", "required_tools", "required_modalities", "deadline_remaining_ms", "budget_remaining_usd", "independent_validator_required"}))
        return cls(project_id=value["project_id"], task_family=value["task_family"], risk_level=value["risk_level"], ood_score=value["ood_score"], required_tools=tuple(value["required_tools"]), required_modalities=tuple(value["required_modalities"]), deadline_remaining_ms=value["deadline_remaining_ms"], budget_remaining_usd=value["budget_remaining_usd"], independent_validator_required=value["independent_validator_required"])

    def to_dict(self) -> dict[str, Any]:
        return {"project_id": self.project_id, "task_family": self.task_family, "risk_level": self.risk_level, "ood_score": self.ood_score, "required_tools": list(self.required_tools), "required_modalities": list(self.required_modalities), "deadline_remaining_ms": self.deadline_remaining_ms, "budget_remaining_usd": self.budget_remaining_usd, "independent_validator_required": self.independent_validator_required}


@dataclass(frozen=True, slots=True)
class RouteAssessmentV1:
    route_id: str
    posterior_id: str
    score: float
    conservative_success_lower: float
    coverage: float
    calibration_status: str
    reasons: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "route_id", bounded_text(self.route_id, "route_id"))
        object.__setattr__(self, "posterior_id", bounded_text(self.posterior_id, "posterior_id"))
        object.__setattr__(self, "score", finite_number(self.score, "score"))
        object.__setattr__(self, "conservative_success_lower", probability(self.conservative_success_lower, "conservative_success_lower"))
        object.__setattr__(self, "coverage", probability(self.coverage, "coverage"))
        if self.calibration_status not in CALIBRATION_STATUSES:
            raise ValueError("unsupported calibration_status")
        object.__setattr__(self, "reasons", unique_texts(self.reasons, "reasons", maximum_items=32))

    def to_dict(self) -> dict[str, Any]:
        return {"route_id": self.route_id, "posterior_id": self.posterior_id, "score": self.score, "conservative_success_lower": self.conservative_success_lower, "coverage": self.coverage, "calibration_status": self.calibration_status, "reasons": list(self.reasons)}


@dataclass(frozen=True, slots=True)
class CalibratedRouteDecisionV1:
    project_id: str
    decision_id: str
    task_family: str
    action: str
    selected_route_id: str | None
    assessments: tuple[RouteAssessmentV1, ...]
    reason_codes: tuple[str, ...]
    ood_score: float
    coverage: float
    calibration_status: str
    evidence_ids: tuple[str, ...]
    created_at: str
    advisory_only: bool = True
    schema_version: str = CAPABILITY_GRAPH_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "project_id", bounded_text(self.project_id, "project_id"))
        object.__setattr__(self, "decision_id", bounded_text(self.decision_id, "decision_id"))
        object.__setattr__(self, "task_family", bounded_text(self.task_family, "task_family", maximum=64))
        if self.action not in ROUTER_ACTIONS:
            raise ValueError("unsupported route action")
        if self.selected_route_id is not None:
            object.__setattr__(self, "selected_route_id", bounded_text(self.selected_route_id, "selected_route_id"))
        if not isinstance(self.assessments, tuple) or len(self.assessments) > 128 or any(not isinstance(item, RouteAssessmentV1) for item in self.assessments):
            raise TypeError("assessments must be a tuple of RouteAssessmentV1")
        object.__setattr__(self, "reason_codes", unique_texts(self.reason_codes, "reason_codes", maximum_items=64))
        object.__setattr__(self, "ood_score", probability(self.ood_score, "ood_score"))
        object.__setattr__(self, "coverage", probability(self.coverage, "coverage"))
        if self.calibration_status not in CALIBRATION_STATUSES:
            raise ValueError("unsupported calibration_status")
        object.__setattr__(self, "evidence_ids", unique_texts(self.evidence_ids, "evidence_ids", maximum_items=128))
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))
        if not isinstance(self.advisory_only, bool) or not self.advisory_only:
            raise ValueError("v0.6.2 route decisions are advisory_only")
        if self.schema_version != CAPABILITY_GRAPH_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {CAPABILITY_GRAPH_SCHEMA_VERSION}")
        if self.action == "ROUTE" and self.selected_route_id is None:
            raise ValueError("ROUTE requires selected_route_id")
        if self.action != "ROUTE" and self.selected_route_id is not None:
            raise ValueError("ABSTAIN/ESCALATE cannot select a route")

    @property
    def trace_hash(self) -> str:
        return stable_digest(self.to_dict(include_hash=False), length=64)

    def to_dict(self, *, include_hash: bool = True) -> dict[str, Any]:
        result = {
            "project_id": self.project_id,
            "decision_id": self.decision_id,
            "task_family": self.task_family,
            "action": self.action,
            "selected_route_id": self.selected_route_id,
            "assessments": [item.to_dict() for item in self.assessments],
            "reason_codes": list(self.reason_codes),
            "ood_score": self.ood_score,
            "coverage": self.coverage,
            "calibration_status": self.calibration_status,
            "evidence_ids": list(self.evidence_ids),
            "created_at": self.created_at,
            "advisory_only": self.advisory_only,
            "schema_version": self.schema_version,
        }
        if include_hash:
            result["trace_hash"] = self.trace_hash
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CalibratedRouteDecisionV1":
        required = frozenset({"project_id", "decision_id", "task_family", "action", "selected_route_id", "assessments", "reason_codes", "ood_score", "coverage", "calibration_status", "evidence_ids", "created_at", "advisory_only", "schema_version"})
        validate_exact_fields(value, required=required, optional=frozenset({"trace_hash"}))
        if value["schema_version"] != CAPABILITY_GRAPH_SCHEMA_VERSION:
            raise ValueError("unsupported route decision schema_version")
        assessments = tuple(
            RouteAssessmentV1(
                route_id=item["route_id"], posterior_id=item["posterior_id"], score=item["score"], conservative_success_lower=item["conservative_success_lower"], coverage=item["coverage"], calibration_status=item["calibration_status"], reasons=tuple(item["reasons"]),
            )
            for item in value["assessments"]
        )
        item = cls(project_id=value["project_id"], decision_id=value["decision_id"], task_family=value["task_family"], action=value["action"], selected_route_id=value["selected_route_id"], assessments=assessments, reason_codes=tuple(value["reason_codes"]), ood_score=value["ood_score"], coverage=value["coverage"], calibration_status=value["calibration_status"], evidence_ids=tuple(value["evidence_ids"]), created_at=value["created_at"], advisory_only=value["advisory_only"], schema_version=value["schema_version"])
        if "trace_hash" in value and value["trace_hash"] != item.trace_hash:
            raise ValueError("trace_hash does not match route decision content")
        return item


@dataclass(frozen=True, slots=True)
class DriftReportV1:
    task_distribution_shift: float
    model_revision_changed: bool
    price_shift: float
    latency_shift: float
    error_rate_shift: float
    freeze_candidate: bool
    reason_codes: tuple[str, ...]
    created_at: str

    def __post_init__(self) -> None:
        for name in ("task_distribution_shift", "price_shift", "latency_shift", "error_rate_shift"):
            object.__setattr__(self, name, probability(getattr(self, name), name))
        if not isinstance(self.model_revision_changed, bool) or not isinstance(self.freeze_candidate, bool):
            raise TypeError("drift flags must be bool")
        object.__setattr__(self, "reason_codes", unique_texts(self.reason_codes, "reason_codes", maximum_items=32))
        object.__setattr__(self, "created_at", validate_timestamp(self.created_at, "created_at"))

    def to_dict(self) -> dict[str, Any]:
        return {"task_distribution_shift": self.task_distribution_shift, "model_revision_changed": self.model_revision_changed, "price_shift": self.price_shift, "latency_shift": self.latency_shift, "error_rate_shift": self.error_rate_shift, "freeze_candidate": self.freeze_candidate, "reason_codes": list(self.reason_codes), "created_at": self.created_at, "schema_version": CAPABILITY_GRAPH_SCHEMA_VERSION}


class CapabilityRouterPlugin(Protocol):
    """Minimal plugin boundary used by offline tournament adapters."""

    plugin_id: str

    def decide(self, *, context: RouteContextV1, candidates: Sequence[RouteCandidateV1], evidence: Sequence[CapabilityEvidenceV1], now: str) -> tuple[CalibratedRouteDecisionV1, tuple[CapabilityPosteriorV2, ...]]:
        """Return the same structured decision surface as the reference router."""


def calibration_metrics(predictions: Sequence[tuple[float, bool]], *, bins: int = 10) -> dict[str, float | int | None]:
    """Compute auditable Brier/ECE and selective risk-coverage diagnostics.

    This helper accepts predictions and independent binary labels only.  It
    never fabricates labels and returns ``None`` for an empty sample.
    """

    if isinstance(bins, bool) or not isinstance(bins, int) or not 2 <= bins <= 50:
        raise ValueError("bins must be between 2 and 50")
    if not predictions:
        return {"sample_count": 0, "brier": None, "ece": None, "risk_coverage": None}
    normalized: list[tuple[float, bool]] = []
    for index, (prediction, label) in enumerate(predictions):
        normalized.append((probability(prediction, f"predictions[{index}].probability"), bool(label)))
    brier = sum((probability - (1.0 if label else 0.0)) ** 2 for probability, label in normalized) / len(normalized)
    ece = 0.0
    for bucket in range(bins):
        lower = bucket / bins
        upper = (bucket + 1) / bins
        members = [(prediction, label) for prediction, label in normalized if lower <= prediction < upper or (bucket == bins - 1 and prediction <= upper)]
        if members:
            ece += len(members) / len(normalized) * abs(sum(prediction for prediction, _ in members) / len(members) - sum(1.0 if label else 0.0 for _, label in members) / len(members))
    # Selective coverage at the common conservative threshold.  This is a
    # diagnostic, not a claim that a production risk target was met.
    selected = [(prediction, label) for prediction, label in normalized if prediction >= 0.5]
    coverage = len(selected) / len(normalized)
    risk = (sum(0 if label else 1 for _, label in selected) / len(selected)) if selected else None
    return {"sample_count": len(normalized), "brier": brier, "ece": ece, "risk_coverage": (coverage if risk is None else coverage * (1.0 - risk))}


def _weighted_interval(rate: float, effective_n: float, *, prior_mean: float = 0.5, prior_strength: float = 2.0, prior_half_width: float = 0.45) -> ProbabilityInterval:
    # Beta-style posterior with a deliberately weak prior.  The interval is
    # widened by the prior uncertainty and never presented as a guarantee.
    total = max(0.0, effective_n) + max(0.0, prior_strength)
    mean = (max(0.0, effective_n) * probability(rate, "rate") + max(0.0, prior_strength) * probability(prior_mean, "prior_mean")) / max(total, 1e-12)
    variance = mean * (1.0 - mean) / (total + 1.0)
    radius = min(0.49, 1.96 * math.sqrt(max(0.0, variance)) + prior_half_width / (1.0 + effective_n))
    return ProbabilityInterval(mean=mean, lower=max(0.0, mean - radius), upper=min(1.0, mean + radius))


class CapabilityCalibrator:
    """Deterministic decayed aggregation and conservative intervals."""

    def __init__(self, *, half_life_days: float = 90.0, stale_after_days: float = 180.0, minimum_effective_samples: float = 5.0) -> None:
        self.half_life_days = _non_negative_float(half_life_days, "half_life_days")
        if self.half_life_days <= 0:
            raise ValueError("half_life_days must be positive")
        self.stale_after_days = _non_negative_float(stale_after_days, "stale_after_days")
        self.minimum_effective_samples = _non_negative_float(minimum_effective_samples, "minimum_effective_samples")

    def posterior(
        self,
        *,
        project_id: str,
        task_family: str,
        model_revision: str,
        reasoning_tier: str,
        provider_deployment: str,
        validator_id: str,
        tool_ids: Sequence[str],
        modality_ids: Sequence[str],
        evidence: Iterable[CapabilityEvidenceV1],
        now: str,
        prior: CapabilityPriorV1 | None = None,
        ood: bool = False,
        calibration_set_id: str = "unavailable",
    ) -> CapabilityPosteriorV2:
        timestamp = validate_timestamp(now, "now")
        prior = prior or CapabilityPriorV1()
        exact = capability_condition_key(task_family=task_family, model_revision=model_revision, reasoning_tier=reasoning_tier, provider_deployment=provider_deployment, tool_ids=tool_ids, modality_ids=modality_ids, validator_id=validator_id)
        rows = [item for item in evidence if item.condition_key == exact and item.project_id == project_id]
        weighted_success = weighted_total = 0.0
        weighted_retry = weighted_escalation = weighted_rework = weighted_corr = 0.0
        weighted_cost = weighted_latency = 0.0
        weighted_cost_p95 = weighted_latency_p95 = 0.0
        effective_n = 0.0
        sample_count = 0
        source_ids: list[str] = []
        max_age = 0.0
        levels: list[str] = []
        expired_count = 0
        for item in rows:
            age = _age_days(item.observed_at, timestamp)
            max_age = max(max_age, age)
            weight = math.exp(-age / self.half_life_days)
            if item.expires_at is not None and _iso_datetime(timestamp) > _iso_datetime(item.expires_at):
                weight = 0.0
                expired_count += 1
            n = float(item.sample_count)
            effective_n += weight * n
            weighted_total += weight * n
            weighted_success += weight * item.successes
            weighted_retry += weight * item.retry_events
            weighted_escalation += weight * item.escalation_events
            weighted_rework += weight * item.rework_events
            weighted_corr += weight * item.correlated_failure_events
            weighted_cost += weight * n * item.cost_usd.mean
            weighted_cost_p95 += weight * n * item.cost_usd.p95
            weighted_latency += weight * n * item.latency_ms.mean
            weighted_latency_p95 += weight * n * item.latency_ms.p95
            sample_count += item.sample_count
            if item.source_id not in source_ids:
                source_ids.append(item.source_id)
            levels.append(item.evidence_level)
        success_rate = weighted_success / weighted_total if weighted_total else prior.success.mean
        retry_rate = weighted_retry / weighted_total if weighted_total else 0.0
        escalation_rate = weighted_escalation / weighted_total if weighted_total else 0.0
        rework_rate = weighted_rework / weighted_total if weighted_total else 0.0
        corr_rate = weighted_corr / weighted_total if weighted_total else 0.0
        cost_mean = (weighted_cost / weighted_total) if weighted_total else prior.cost_usd.mean
        cost_p95 = (weighted_cost_p95 / weighted_total) if weighted_total else prior.cost_usd.p95
        latency_mean = (weighted_latency / weighted_total) if weighted_total else prior.latency_ms.mean
        latency_p95 = (weighted_latency_p95 / weighted_total) if weighted_total else prior.latency_ms.p95
        status = "calibrated" if effective_n >= self.minimum_effective_samples else "prior"
        stale = bool(rows) and (max_age > self.stale_after_days or expired_count == len(rows))
        if stale:
            status = "stale"
        if ood:
            status = "ood"
        if not rows and status == "prior":
            sample_count = 0
        coverage = min(1.0, effective_n / max(1.0, self.minimum_effective_samples))
        level = max(levels, key=lambda item: (levels.index(item), item)) if levels else "E0-simulated"
        return CapabilityPosteriorV2(
            project_id=project_id, task_family=task_family, model_revision=model_revision, reasoning_tier=reasoning_tier, provider_deployment=provider_deployment, validator_id=validator_id,
            tool_ids=tuple(tool_ids), modality_ids=tuple(modality_ids), success=_weighted_interval(success_rate, effective_n, prior_mean=prior.success.mean, prior_strength=prior.strength, prior_half_width=max(prior.success.upper - prior.success.lower, 0.05) / 2),
            cost_usd=NonNegativeEstimate(mean=max(0.0, cost_mean), p95=max(max(0.0, cost_mean), max(0.0, cost_p95))), latency_ms=NonNegativeEstimate(mean=max(0.0, latency_mean), p95=max(max(0.0, latency_mean), max(0.0, latency_p95))),
            retry_probability=_weighted_interval(retry_rate, effective_n, prior_mean=0.0, prior_strength=prior.strength, prior_half_width=0.35), escalation_probability=_weighted_interval(escalation_rate, effective_n, prior_mean=0.0, prior_strength=prior.strength, prior_half_width=0.35), rework_probability=_weighted_interval(rework_rate, effective_n, prior_mean=0.0, prior_strength=prior.strength, prior_half_width=0.35), failure_correlation=_weighted_interval(corr_rate, effective_n, prior_mean=0.0, prior_strength=prior.strength, prior_half_width=0.35),
            sample_count=sample_count, effective_sample_size=effective_n, evidence_level=level, calibration_status=status, coverage=coverage, stale=stale, ood=ood, prior_source=prior.source, source_ids=tuple(source_ids), as_of=timestamp, calibration_set_id=calibration_set_id, cost_known=bool(rows) or prior.cost_known, latency_known=bool(rows) or prior.latency_known,
        )


class CapabilityGraphRouter:
    """Conservative route selector over graph-calibrated posteriors."""

    def __init__(self, calibrator: CapabilityCalibrator | None = None) -> None:
        self.calibrator = calibrator or CapabilityCalibrator()

    @staticmethod
    def _prior_for(candidate: RouteCandidateV1) -> CapabilityPriorV1:
        # Metadata can seed a wide prior, but never a narrow success claim.
        tier = candidate.reasoning_tier.lower()
        mean = 0.56 if tier in {"high", "xhigh", "expert"} else 0.50
        return CapabilityPriorV1(
            success=ProbabilityInterval(mean=mean, lower=max(0.05, mean - 0.42), upper=min(0.95, mean + 0.42)),
            cost_usd=candidate.base_cost_usd,
            latency_ms=candidate.base_latency_ms,
            source="model-family-metadata-wide-prior",
            strength=2.0,
            cost_known=candidate.base_cost_known,
            latency_known=candidate.base_latency_known,
        )

    def decide(
        self,
        *,
        context: RouteContextV1,
        candidates: Sequence[RouteCandidateV1],
        evidence: Sequence[CapabilityEvidenceV1],
        now: str,
        decision_id: str | None = None,
        calibration_set_id: str = "unavailable",
    ) -> tuple[CalibratedRouteDecisionV1, tuple[CapabilityPosteriorV2, ...]]:
        if not candidates:
            decision = CalibratedRouteDecisionV1(project_id=context.project_id, decision_id=decision_id or stable_id("capability_decision", {"project_id": context.project_id, "task_family": context.task_family, "now": now, "empty": True}), task_family=context.task_family, action="ABSTAIN", selected_route_id=None, assessments=(), reason_codes=("no_candidates",), ood_score=context.ood_score, coverage=0.0, calibration_status="insufficient", evidence_ids=(), created_at=now)
            return decision, ()
        posteriors: list[CapabilityPosteriorV2] = []
        assessments: list[RouteAssessmentV1] = []
        reasons: set[str] = set()
        for candidate in candidates:
            matching = [item for item in evidence if item.project_id == context.project_id and item.task_family == context.task_family and item.model_revision == candidate.model_revision and item.reasoning_tier == candidate.reasoning_tier and item.provider_deployment == candidate.provider_deployment and item.tool_ids == candidate.tool_ids and item.modality_ids == candidate.modality_ids and item.validator_id == candidate.validator_id]
            posterior = self.calibrator.posterior(project_id=context.project_id, task_family=context.task_family, model_revision=candidate.model_revision, reasoning_tier=candidate.reasoning_tier, provider_deployment=candidate.provider_deployment, validator_id=candidate.validator_id, tool_ids=candidate.tool_ids, modality_ids=candidate.modality_ids, evidence=matching, now=now, prior=self._prior_for(candidate), ood=context.ood_score >= 0.5, calibration_set_id=calibration_set_id)
            posteriors.append(posterior)
            score = posterior.success.lower
            score -= 0.18 * posterior.retry_probability.upper
            score -= 0.22 * posterior.escalation_probability.upper
            score -= 0.16 * posterior.rework_probability.upper
            score -= 0.10 * posterior.failure_correlation.upper
            score -= 0.04 * (1.0 - posterior.coverage)
            if context.budget_remaining_usd and posterior.cost_usd.p95 > context.budget_remaining_usd:
                score -= 0.35
            if context.deadline_remaining_ms and posterior.latency_ms.p95 > context.deadline_remaining_ms:
                score -= 0.35
            local_reasons = [posterior.calibration_status]
            if posterior.sample_count == 0:
                local_reasons.append("cold_start_prior")
            if posterior.failure_correlation.upper >= 0.35:
                local_reasons.append("correlated_failure_risk")
            if not posterior.cost_known:
                local_reasons.append("cost_unknown")
            if not posterior.latency_known:
                local_reasons.append("latency_unknown")
            assessments.append(RouteAssessmentV1(route_id=candidate.route_id, posterior_id=posterior.posterior_id, score=score, conservative_success_lower=posterior.success.lower, coverage=posterior.coverage, calibration_status=posterior.calibration_status, reasons=tuple(local_reasons)))
        ranked = sorted(zip(assessments, posteriors, candidates), key=lambda row: (-row[0].score, row[0].route_id))
        best_assessment, best_posterior, best_candidate = ranked[0]
        if context.ood_score >= 0.5:
            reasons.add("ood_task")
        if context.risk_level in {"high", "critical"}:
            reasons.add("high_risk_requires_conservative_evidence")
        if best_posterior.stale:
            reasons.add("stale_evidence")
        if best_posterior.coverage < 0.2:
            reasons.add("low_coverage")
        if context.independent_validator_required and best_candidate.validator_id == "validator:none":
            reasons.add("independent_validator_missing")
        if not best_posterior.cost_known:
            reasons.add("cost_unknown")
        if not best_posterior.latency_known:
            reasons.add("latency_unknown")
        if best_posterior.failure_correlation.upper >= 0.6:
            reasons.add("failure_correlation_too_high")
        hard_stop = (
            context.ood_score >= 0.8
            or context.risk_level == "critical"
            or best_posterior.stale
            or (context.independent_validator_required and best_candidate.validator_id == "validator:none")
            or (best_posterior.coverage < 0.2 and context.risk_level in {"high", "critical"})
            or (context.risk_level in {"high", "critical"} and (not best_posterior.cost_known or not best_posterior.latency_known))
        )
        if hard_stop:
            action = "ESCALATE"
            selected = None
            reasons.add("calibration_boundary")
        elif best_assessment.score <= 0.15:
            action = "ABSTAIN"
            selected = None
            reasons.add("no_conservative_route")
        else:
            action = "ROUTE"
            selected = best_candidate.route_id
            if best_posterior.calibration_status == "prior":
                reasons.add("route_uses_wide_prior")
        status = best_posterior.calibration_status
        evidence_ids = tuple(sorted({source for posterior in posteriors for source in posterior.source_ids}))
        decision = CalibratedRouteDecisionV1(project_id=context.project_id, decision_id=decision_id or stable_id("capability_decision", {"project_id": context.project_id, "task_family": context.task_family, "assessments": [item.to_dict() for item in assessments], "action": action}), task_family=context.task_family, action=action, selected_route_id=selected, assessments=tuple(item for item, _, _ in ranked), reason_codes=tuple(sorted(reasons)), ood_score=context.ood_score, coverage=best_posterior.coverage, calibration_status=status, evidence_ids=evidence_ids, created_at=now)
        return decision, tuple(posteriors)


def detect_drift(
    *,
    baseline: Mapping[str, Any],
    current: Mapping[str, Any],
    now: str,
    threshold: float = 0.25,
) -> DriftReportV1:
    """Compare normalized telemetry summaries without treating missing data as zero."""

    threshold = probability(threshold, "threshold")
    def shift(name: str) -> float:
        left = baseline.get(name)
        right = current.get(name)
        if left is None or right is None:
            return 1.0
        left_n = _non_negative_float(left, f"baseline.{name}")
        right_n = _non_negative_float(right, f"current.{name}")
        return min(1.0, abs(right_n - left_n) / max(1.0, abs(left_n)))

    task_shift = shift("task_distribution")
    price_shift = shift("price")
    latency_shift = shift("latency")
    error_shift = shift("error_rate")
    model_changed = baseline.get("model_revision") is not None and current.get("model_revision") is not None and baseline.get("model_revision") != current.get("model_revision")
    reasons: list[str] = []
    if task_shift > threshold: reasons.append("task_distribution_drift")
    if price_shift > threshold: reasons.append("price_drift")
    if latency_shift > threshold: reasons.append("latency_drift")
    if error_shift > threshold: reasons.append("error_rate_drift")
    if model_changed: reasons.append("model_revision_drift")
    return DriftReportV1(task_distribution_shift=task_shift, model_revision_changed=model_changed, price_shift=price_shift, latency_shift=latency_shift, error_rate_shift=error_shift, freeze_candidate=bool(reasons), reason_codes=tuple(reasons), created_at=now)


__all__ = [
    "CALIBRATION_REVISION",
    "CALIBRATION_STATUSES",
    "CAPABILITY_GRAPH_POLICY_REVISION",
    "CAPABILITY_GRAPH_SCHEMA_VERSION",
    "CAPABILITY_NODE_TYPES",
    "EVIDENCE_SOURCE_KINDS",
    "ROUTER_ACTIONS",
    "CapabilityCalibrator",
    "CapabilityEvidenceV1",
    "CapabilityGraphRouter",
    "CapabilityNodeV1",
    "CapabilityPosteriorV2",
    "CapabilityPriorV1",
    "CapabilityRouterPlugin",
    "CalibratedRouteDecisionV1",
    "DriftReportV1",
    "RouteAssessmentV1",
    "RouteCandidateV1",
    "RouteContextV1",
    "capability_condition_key",
    "calibration_metrics",
    "detect_drift",
]
