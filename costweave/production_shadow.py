"""Failure-isolated, production-like shadow evaluation for v0.5.7.2."""

from __future__ import annotations

import hashlib
import math
import statistics
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

from .evaluation_protocol import canonical_json


PRODUCTION_SHADOW_SCHEMA_VERSION = "v0.5.7.2"
PRODUCTION_SHADOW_POLICY_REVISION = "production-like-shadow-v1"
SHADOW_INPUT_SCHEMA_VERSION = "production-shadow-input-v1"
SHADOW_OUTCOME_SCHEMA_VERSION = "production-shadow-outcome-v1"

MAXIMUM_SHADOW_CANDIDATES = 64
MAXIMUM_SHADOW_BATCH = 500
MINIMUM_DIRECTIONAL_SHADOW_CASES = 30
MAXIMUM_SHADOW_OVERHEAD_RATIO = 0.10
MAXIMUM_CAPACITY_AGE_MS = 30_000.0

SELECTION_RULES = frozenset(
    {
        "minimum_expected_loss",
        "minimum_cost",
        "maximum_quality_lower",
        "minimum_latency_p95",
        "frozen_score",
    }
)
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})
REAL_DATA_ORIGINS = frozenset({"production", "human_benchmark"})
ALL_DATA_ORIGINS = REAL_DATA_ORIGINS | {"test_fixture", "simulated"}
MISSING_REASONS = frozenset(
    {
        "capacity_snapshot_stale",
        "no_candidates",
        "no_feasible_candidate",
        "shadow_budget_exceeded",
        "shadow_deadline_exceeded",
    }
)


class ProductionShadowError(ValueError):
    """Raised when shadow inputs or evidence violate the frozen contract."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _bounded(value: Any, name: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 160 or "\n" in text or "\r" in text:
        raise ProductionShadowError(f"{name} must be a bounded identifier")
    lowered = text.lower()
    if any(marker in lowered for marker in ("bearer ", "api_key=", "password=", "secret=")):
        raise ProductionShadowError(f"{name} appears to contain a secret")
    return text


def _sha256(value: Any, name: str) -> str:
    text = str(value).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise ProductionShadowError(f"{name} must be a sha256 digest")
    return text


def _finite(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ProductionShadowError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise ProductionShadowError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise ProductionShadowError(f"{name} is below its minimum")
    if maximum is not None and number > maximum:
        raise ProductionShadowError(f"{name} exceeds its maximum")
    return number


def _timestamp(value: str, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProductionShadowError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise ProductionShadowError(f"{name} must include timezone")
    return str(value)


def _anonymous(value: str, prefix: str, name: str) -> str:
    text = str(value)
    suffix = text.removeprefix(prefix)
    if (
        not text.startswith(prefix)
        or len(suffix) != 32
        or any(char not in "0123456789abcdef" for char in suffix.lower())
    ):
        raise ProductionShadowError(f"{name} must be anonymous")
    return text


def _evidence_rank(value: str) -> int:
    prefix = str(value).split("-", 1)[0].upper()
    return {"E0": 0, "E1": 1, "E2": 2, "E3": 3, "E4": 4, "E5": 5}.get(prefix, -1)


def _percentile(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ProductionShadowError("percentile requires observations")
    position = (len(ordered) - 1) * percentile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _validate_task_features(value: Mapping[str, Any]) -> dict[str, Any]:
    prohibited = {
        "goal",
        "prompt",
        "raw_input",
        "raw_output",
        "user_id",
        "project_id",
        "api_key",
        "authorization",
    }
    if not value or len(value) > 64:
        raise ProductionShadowError("shadow task features are empty or excessive")
    result: dict[str, Any] = {}
    for key, item in sorted(value.items()):
        name = _bounded(key, "task_feature_name")
        if name.lower() in prohibited:
            raise ProductionShadowError("raw or identifying shadow features are forbidden")
        if isinstance(item, bool) or item is None:
            result[name] = item
        elif isinstance(item, (int, float)):
            result[name] = _finite(item, f"task_feature.{name}")
        elif isinstance(item, str):
            result[name] = _bounded(item, f"task_feature.{name}")
        elif isinstance(item, (list, tuple)):
            if len(item) > 32:
                raise ProductionShadowError("shadow feature list is excessive")
            result[name] = [_bounded(entry, f"task_feature.{name}") for entry in item]
        else:
            raise ProductionShadowError("shadow task feature type is unsupported")
    return result


@dataclass(frozen=True, slots=True)
class ProductionShadowInput:
    input_id: str
    anonymous_task_id: str
    task_feature_schema_version: str
    task_features: Mapping[str, Any]
    task_feature_hash: str
    catalog_revision: str
    catalog_snapshot_hash: str
    capacity_revision: str
    capacity_snapshot_hash: str
    capacity_captured_at: str
    capacity_age_ms: float
    capacity_freshness_limit_ms: float
    actual_route_id: str
    actual_route_fingerprint: str
    baseline_predicted_quality: float
    baseline_predicted_total_cost_usd: float
    baseline_predicted_latency_p95_ms: float
    baseline_end_to_end_estimated_cost_usd: float
    baseline_end_to_end_estimated_latency_ms: float
    end_to_end_cost_budget_usd: float
    end_to_end_latency_budget_ms: float
    risk_level: str
    reversible: bool
    permission_sensitive: bool
    evidence_level: str
    data_origin: str
    input_hash: str
    same_features_as_baseline: bool = True
    same_catalog_as_baseline: bool = True
    same_capacity_as_baseline: bool = True
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    schema_version: str = SHADOW_INPUT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SHADOW_INPUT_SCHEMA_VERSION:
            raise ProductionShadowError("production shadow input schema is invalid")
        _anonymous(self.anonymous_task_id, "task_", "shadow task id")
        for value, name in (
            (self.task_feature_schema_version, "task_feature_schema_version"),
            (self.catalog_revision, "catalog_revision"),
            (self.capacity_revision, "capacity_revision"),
            (self.actual_route_id, "actual_route_id"),
        ):
            _bounded(value, name)
        features = _validate_task_features(self.task_features)
        if canonical_json(features) != canonical_json(dict(self.task_features)):
            raise ProductionShadowError("shadow task features are not canonical")
        _sha256(self.task_feature_hash, "task_feature_hash")
        _sha256(self.catalog_snapshot_hash, "catalog_snapshot_hash")
        _sha256(self.capacity_snapshot_hash, "capacity_snapshot_hash")
        _sha256(self.actual_route_fingerprint, "actual_route_fingerprint")
        _sha256(self.input_hash, "shadow_input_hash")
        _timestamp(self.capacity_captured_at, "capacity_captured_at")
        _finite(self.capacity_age_ms, "capacity_age_ms", minimum=0.0)
        _finite(
            self.capacity_freshness_limit_ms,
            "capacity_freshness_limit_ms",
            minimum=1.0,
            maximum=MAXIMUM_CAPACITY_AGE_MS,
        )
        _finite(self.baseline_predicted_quality, "baseline_predicted_quality", minimum=0.0, maximum=1.0)
        _finite(self.baseline_predicted_total_cost_usd, "baseline_predicted_total_cost_usd", minimum=0.0)
        _finite(self.baseline_predicted_latency_p95_ms, "baseline_predicted_latency_p95_ms", minimum=0.0)
        _finite(self.baseline_end_to_end_estimated_cost_usd, "baseline_end_to_end_estimated_cost_usd", minimum=1e-12)
        _finite(self.baseline_end_to_end_estimated_latency_ms, "baseline_end_to_end_estimated_latency_ms", minimum=1e-12)
        _finite(self.end_to_end_cost_budget_usd, "end_to_end_cost_budget_usd", minimum=0.0)
        _finite(self.end_to_end_latency_budget_ms, "end_to_end_latency_budget_ms", minimum=1.0)
        if self.risk_level not in RISK_LEVELS:
            raise ProductionShadowError("shadow risk level is invalid")
        if _evidence_rank(self.evidence_level) < 0 or self.data_origin not in ALL_DATA_ORIGINS:
            raise ProductionShadowError("shadow input evidence is invalid")
        if (
            not self.same_features_as_baseline
            or not self.same_catalog_as_baseline
            or not self.same_capacity_as_baseline
            or not self.advisory_only
            or self.routing_authority
            or self.may_change_actual_route
        ):
            raise ProductionShadowError("shadow input is not isolated from baseline execution")
        if self.task_feature_hash != _digest(features):
            raise ProductionShadowError("shadow task feature hash is invalid")
        if self.input_hash != _digest(self.payload()):
            raise ProductionShadowError("production shadow input hash is invalid")
        if self.input_id != "shadowinput_" + self.input_hash[:32]:
            raise ProductionShadowError("production shadow input id is invalid")

    @property
    def capacity_fresh(self) -> bool:
        return self.capacity_age_ms <= self.capacity_freshness_limit_ms

    def payload(self) -> dict[str, Any]:
        return {
            "anonymous_task_id": self.anonymous_task_id,
            "task_feature_schema_version": self.task_feature_schema_version,
            "task_features": dict(sorted(self.task_features.items())),
            "task_feature_hash": self.task_feature_hash,
            "catalog_revision": self.catalog_revision,
            "catalog_snapshot_hash": self.catalog_snapshot_hash,
            "capacity_revision": self.capacity_revision,
            "capacity_snapshot_hash": self.capacity_snapshot_hash,
            "capacity_captured_at": self.capacity_captured_at,
            "capacity_age_ms": self.capacity_age_ms,
            "capacity_freshness_limit_ms": self.capacity_freshness_limit_ms,
            "actual_route_id": self.actual_route_id,
            "actual_route_fingerprint": self.actual_route_fingerprint,
            "baseline_predicted_quality": self.baseline_predicted_quality,
            "baseline_predicted_total_cost_usd": self.baseline_predicted_total_cost_usd,
            "baseline_predicted_latency_p95_ms": self.baseline_predicted_latency_p95_ms,
            "baseline_end_to_end_estimated_cost_usd": self.baseline_end_to_end_estimated_cost_usd,
            "baseline_end_to_end_estimated_latency_ms": self.baseline_end_to_end_estimated_latency_ms,
            "end_to_end_cost_budget_usd": self.end_to_end_cost_budget_usd,
            "end_to_end_latency_budget_ms": self.end_to_end_latency_budget_ms,
            "risk_level": self.risk_level,
            "reversible": self.reversible,
            "permission_sensitive": self.permission_sensitive,
            "evidence_level": self.evidence_level,
            "data_origin": self.data_origin,
            "same_features_as_baseline": self.same_features_as_baseline,
            "same_catalog_as_baseline": self.same_catalog_as_baseline,
            "same_capacity_as_baseline": self.same_capacity_as_baseline,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_id": self.input_id,
            **self.payload(),
            "input_hash": self.input_hash,
            "capacity_fresh": self.capacity_fresh,
        }

    @classmethod
    def create(cls, **values: Any) -> "ProductionShadowInput":
        features = _validate_task_features(values["task_features"])
        payload = {
            **values,
            "task_features": features,
            "task_feature_hash": _digest(features),
            "same_features_as_baseline": True,
            "same_catalog_as_baseline": True,
            "same_capacity_as_baseline": True,
            "advisory_only": True,
            "routing_authority": False,
            "may_change_actual_route": False,
            "schema_version": SHADOW_INPUT_SCHEMA_VERSION,
        }
        input_hash = _digest(payload)
        return cls(
            input_id="shadowinput_" + input_hash[:32],
            input_hash=input_hash,
            **payload,
        )


@dataclass(frozen=True, slots=True)
class ProductionShadowCandidate:
    route_id: str
    route_fingerprint: str
    capacity_snapshot_hash: str
    predicted_quality_lower: float
    expected_total_cost_usd: float
    predicted_latency_p95_ms: float
    expected_total_loss: float
    hard_constraints_passed: bool
    capacity_eligible: bool

    def __post_init__(self) -> None:
        _bounded(self.route_id, "shadow_candidate_route_id")
        _sha256(self.route_fingerprint, "shadow_candidate_fingerprint")
        _sha256(self.capacity_snapshot_hash, "candidate_capacity_snapshot_hash")
        _finite(self.predicted_quality_lower, "predicted_quality_lower", minimum=0.0, maximum=1.0)
        _finite(self.expected_total_cost_usd, "expected_total_cost_usd", minimum=0.0)
        _finite(self.predicted_latency_p95_ms, "predicted_latency_p95_ms", minimum=0.0)
        _finite(self.expected_total_loss, "expected_total_loss", minimum=0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "route_fingerprint": self.route_fingerprint,
            "capacity_snapshot_hash": self.capacity_snapshot_hash,
            "predicted_quality_lower": self.predicted_quality_lower,
            "expected_total_cost_usd": self.expected_total_cost_usd,
            "predicted_latency_p95_ms": self.predicted_latency_p95_ms,
            "expected_total_loss": self.expected_total_loss,
            "hard_constraints_passed": self.hard_constraints_passed,
            "capacity_eligible": self.capacity_eligible,
        }


@dataclass(frozen=True, slots=True)
class ProductionShadowPolicy:
    policy_id: str
    policy_version: str
    selection_rule: str
    frozen_route_scores: Mapping[str, float] | None = None

    def __post_init__(self) -> None:
        _bounded(self.policy_id, "shadow_policy_id")
        _bounded(self.policy_version, "shadow_policy_version")
        if self.selection_rule not in SELECTION_RULES:
            raise ProductionShadowError("shadow selection rule is invalid")
        if self.selection_rule == "frozen_score":
            if not self.frozen_route_scores:
                raise ProductionShadowError("frozen shadow policy requires route scores")
            for route_id, score in self.frozen_route_scores.items():
                _bounded(route_id, "frozen_shadow_route_id")
                _finite(score, "frozen_shadow_route_score")
        elif self.frozen_route_scores is not None:
            raise ProductionShadowError("route scores only belong to frozen shadow policy")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "selection_rule": self.selection_rule,
            "frozen_route_scores": None if self.frozen_route_scores is None else dict(sorted(self.frozen_route_scores.items())),
        }


@dataclass(frozen=True, slots=True)
class ProductionShadowDecision:
    decision_id: str
    input_id: str
    input_hash: str
    policy_id: str
    policy_version: str
    selection_rule: str
    actual_route_id: str
    actual_route_fingerprint: str
    state: str
    selected_route_id: str | None
    candidate_order: tuple[str, ...]
    missing_reason: str | None
    suggestion: Mapping[str, Any] | None
    shadow_compute_cost_usd: float
    shadow_compute_latency_ms: float
    cost_overhead_ratio: float
    latency_overhead_ratio: float
    cost_overhead_principle_passed: bool
    decision_fingerprint: str
    baseline_execution_continues: bool = True
    actual_route_changed: bool = False
    external_call_count: int = 0
    execution_side_effect_count: int = 0
    advisory_only: bool = True
    routing_authority: bool = False
    policy_promotion_allowed: bool = False
    schema_version: str = PRODUCTION_SHADOW_SCHEMA_VERSION
    policy_revision: str = PRODUCTION_SHADOW_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.schema_version != PRODUCTION_SHADOW_SCHEMA_VERSION or self.policy_revision != PRODUCTION_SHADOW_POLICY_REVISION:
            raise ProductionShadowError("production shadow decision schema is invalid")
        if self.state not in {"advice_generated", "advice_missing"}:
            raise ProductionShadowError("production shadow decision state is invalid")
        if (self.state == "advice_generated") != (self.selected_route_id is not None and self.suggestion is not None):
            raise ProductionShadowError("shadow advice state is inconsistent")
        if self.state == "advice_missing" and self.missing_reason not in MISSING_REASONS:
            raise ProductionShadowError("shadow missing reason is invalid")
        if self.state == "advice_generated" and self.missing_reason is not None:
            raise ProductionShadowError("generated shadow advice cannot have a missing reason")
        _sha256(self.input_hash, "shadow_decision_input_hash")
        _sha256(self.actual_route_fingerprint, "shadow_actual_route_fingerprint")
        _sha256(self.decision_fingerprint, "shadow_decision_fingerprint")
        if len(self.candidate_order) != len(set(self.candidate_order)):
            raise ProductionShadowError("shadow candidate order contains duplicates")
        _finite(self.shadow_compute_cost_usd, "shadow_compute_cost_usd", minimum=0.0)
        _finite(self.shadow_compute_latency_ms, "shadow_compute_latency_ms", minimum=0.0)
        _finite(self.cost_overhead_ratio, "shadow_cost_overhead_ratio", minimum=0.0)
        _finite(self.latency_overhead_ratio, "shadow_latency_overhead_ratio", minimum=0.0)
        if self.cost_overhead_principle_passed != (self.cost_overhead_ratio <= MAXIMUM_SHADOW_OVERHEAD_RATIO + 1e-12):
            raise ProductionShadowError("shadow cost overhead gate is inconsistent")
        if (
            not self.baseline_execution_continues
            or self.actual_route_changed
            or self.external_call_count != 0
            or self.execution_side_effect_count != 0
            or not self.advisory_only
            or self.routing_authority
            or self.policy_promotion_allowed
        ):
            raise ProductionShadowError("production shadow decision cannot affect execution")
        if self.decision_id != "prodshadow_" + _digest(self.identity())[:32]:
            raise ProductionShadowError("production shadow decision id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "input_id": self.input_id,
            "input_hash": self.input_hash,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "selection_rule": self.selection_rule,
            "actual_route_id": self.actual_route_id,
            "actual_route_fingerprint": self.actual_route_fingerprint,
            "state": self.state,
            "selected_route_id": self.selected_route_id,
            "candidate_order": list(self.candidate_order),
            "missing_reason": self.missing_reason,
            "suggestion": None if self.suggestion is None else dict(sorted(self.suggestion.items())),
            "shadow_compute_cost_usd": self.shadow_compute_cost_usd,
            "shadow_compute_latency_ms": self.shadow_compute_latency_ms,
            "cost_overhead_ratio": self.cost_overhead_ratio,
            "latency_overhead_ratio": self.latency_overhead_ratio,
            "cost_overhead_principle_passed": self.cost_overhead_principle_passed,
            "decision_fingerprint": self.decision_fingerprint,
            "baseline_execution_continues": self.baseline_execution_continues,
            "actual_route_changed": self.actual_route_changed,
            "external_call_count": self.external_call_count,
            "execution_side_effect_count": self.execution_side_effect_count,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "policy_promotion_allowed": self.policy_promotion_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"decision_id": self.decision_id, **self.identity()}


@dataclass(frozen=True, slots=True)
class ProductionShadowFailureReceipt:
    failure_id: str
    input_id: str
    input_hash: str
    policy_id: str
    policy_version: str
    actual_route_id: str
    actual_route_fingerprint: str
    error_code: str
    shadow_compute_cost_usd: float
    shadow_compute_latency_ms: float
    baseline_execution_continues: bool = True
    actual_route_changed: bool = False
    error_message_persisted: bool = False
    external_call_count: int = 0
    execution_side_effect_count: int = 0
    advisory_only: bool = True
    routing_authority: bool = False
    schema_version: str = PRODUCTION_SHADOW_SCHEMA_VERSION
    policy_revision: str = PRODUCTION_SHADOW_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.error_code not in {"validation_error", "shadow_internal_error"}:
            raise ProductionShadowError("shadow failure code is invalid")
        _sha256(self.input_hash, "shadow_failure_input_hash")
        _sha256(self.actual_route_fingerprint, "shadow_failure_actual_route_fingerprint")
        _finite(self.shadow_compute_cost_usd, "failure_compute_cost", minimum=0.0)
        _finite(self.shadow_compute_latency_ms, "failure_compute_latency", minimum=0.0)
        if (
            not self.baseline_execution_continues
            or self.actual_route_changed
            or self.error_message_persisted
            or self.external_call_count != 0
            or self.execution_side_effect_count != 0
            or not self.advisory_only
            or self.routing_authority
        ):
            raise ProductionShadowError("shadow failure must be isolated")
        if self.failure_id != "shadowfail_" + _digest(self.identity())[:32]:
            raise ProductionShadowError("shadow failure id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "input_id": self.input_id,
            "input_hash": self.input_hash,
            "policy_id": self.policy_id,
            "policy_version": self.policy_version,
            "actual_route_id": self.actual_route_id,
            "actual_route_fingerprint": self.actual_route_fingerprint,
            "error_code": self.error_code,
            "shadow_compute_cost_usd": self.shadow_compute_cost_usd,
            "shadow_compute_latency_ms": self.shadow_compute_latency_ms,
            "baseline_execution_continues": self.baseline_execution_continues,
            "actual_route_changed": self.actual_route_changed,
            "error_message_persisted": self.error_message_persisted,
            "external_call_count": self.external_call_count,
            "execution_side_effect_count": self.execution_side_effect_count,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"failure_id": self.failure_id, **self.identity()}


class ProductionLikeShadowEngine:
    def observe(
        self,
        shadow_input: ProductionShadowInput,
        candidates: Sequence[ProductionShadowCandidate],
        policy: ProductionShadowPolicy,
        *,
        shadow_compute_cost_usd: float,
        shadow_compute_latency_ms: float,
    ) -> ProductionShadowDecision:
        compute_cost = _finite(shadow_compute_cost_usd, "shadow_compute_cost_usd", minimum=0.0)
        compute_latency = _finite(shadow_compute_latency_ms, "shadow_compute_latency_ms", minimum=0.0)
        items = tuple(sorted(candidates, key=lambda item: item.route_id))
        if len(items) > MAXIMUM_SHADOW_CANDIDATES:
            raise ProductionShadowError("too many shadow candidates")
        if len({item.route_id for item in items}) != len(items):
            raise ProductionShadowError("shadow candidate route ids must be unique")
        if any(item.capacity_snapshot_hash != shadow_input.capacity_snapshot_hash for item in items):
            raise ProductionShadowError("shadow candidate capacity snapshot differs from baseline")
        if policy.selection_rule == "frozen_score":
            assert policy.frozen_route_scores is not None
            if set(policy.frozen_route_scores) != {item.route_id for item in items}:
                raise ProductionShadowError("frozen shadow scores must cover every candidate")

        cost_ratio = compute_cost / shadow_input.baseline_end_to_end_estimated_cost_usd
        latency_ratio = compute_latency / shadow_input.baseline_end_to_end_estimated_latency_ms
        missing_reason: str | None = None
        if not shadow_input.capacity_fresh:
            missing_reason = "capacity_snapshot_stale"
        elif not items:
            missing_reason = "no_candidates"
        elif compute_cost > shadow_input.end_to_end_cost_budget_usd:
            missing_reason = "shadow_budget_exceeded"
        elif compute_latency > shadow_input.end_to_end_latency_budget_ms:
            missing_reason = "shadow_deadline_exceeded"

        feasible = tuple(item for item in items if item.hard_constraints_passed and item.capacity_eligible)
        if missing_reason is None and not feasible:
            missing_reason = "no_feasible_candidate"

        def ordering(item: ProductionShadowCandidate) -> tuple[Any, ...]:
            if policy.selection_rule == "minimum_expected_loss":
                return (item.expected_total_loss, item.route_id)
            if policy.selection_rule == "minimum_cost":
                return (item.expected_total_cost_usd, item.expected_total_loss, item.route_id)
            if policy.selection_rule == "maximum_quality_lower":
                return (-item.predicted_quality_lower, item.expected_total_loss, item.route_id)
            if policy.selection_rule == "minimum_latency_p95":
                return (item.predicted_latency_p95_ms, item.expected_total_loss, item.route_id)
            assert policy.frozen_route_scores is not None
            return (float(policy.frozen_route_scores[item.route_id]), item.route_id)

        ordered = tuple(sorted(feasible, key=ordering)) if missing_reason is None else ()
        selected = ordered[0] if ordered else None
        state = "advice_generated" if selected is not None else "advice_missing"
        suggestion = None if selected is None else {
            "route_id": selected.route_id,
            "route_fingerprint": selected.route_fingerprint,
            "predicted_quality_lower": selected.predicted_quality_lower,
            "expected_total_cost_usd": selected.expected_total_cost_usd,
            "predicted_latency_p95_ms": selected.predicted_latency_p95_ms,
            "expected_total_loss": selected.expected_total_loss,
            "hard_constraints_passed": selected.hard_constraints_passed,
            "capacity_eligible": selected.capacity_eligible,
            "predicted_quality_delta_vs_baseline": round(selected.predicted_quality_lower - shadow_input.baseline_predicted_quality, 10),
            "predicted_cost_delta_vs_baseline_usd": round(selected.expected_total_cost_usd - shadow_input.baseline_predicted_total_cost_usd, 10),
            "predicted_latency_delta_vs_baseline_ms": round(selected.predicted_latency_p95_ms - shadow_input.baseline_predicted_latency_p95_ms, 10),
            "applied_to_actual_route": False,
        }
        decision_fingerprint = _digest(
            {
                "input_hash": shadow_input.input_hash,
                "policy": policy.to_dict(),
                "candidates": [item.to_dict() for item in items],
                "candidate_order": [item.route_id for item in ordered],
                "selected_route_id": None if selected is None else selected.route_id,
                "missing_reason": missing_reason,
            }
        )
        identity = {
            "input_id": shadow_input.input_id,
            "input_hash": shadow_input.input_hash,
            "policy_id": policy.policy_id,
            "policy_version": policy.policy_version,
            "selection_rule": policy.selection_rule,
            "actual_route_id": shadow_input.actual_route_id,
            "actual_route_fingerprint": shadow_input.actual_route_fingerprint,
            "state": state,
            "selected_route_id": None if selected is None else selected.route_id,
            "candidate_order": [item.route_id for item in ordered],
            "missing_reason": missing_reason,
            "suggestion": None if suggestion is None else dict(sorted(suggestion.items())),
            "shadow_compute_cost_usd": compute_cost,
            "shadow_compute_latency_ms": compute_latency,
            "cost_overhead_ratio": round(cost_ratio, 10),
            "latency_overhead_ratio": round(latency_ratio, 10),
            "cost_overhead_principle_passed": cost_ratio <= MAXIMUM_SHADOW_OVERHEAD_RATIO + 1e-12,
            "decision_fingerprint": decision_fingerprint,
            "baseline_execution_continues": True,
            "actual_route_changed": False,
            "external_call_count": 0,
            "execution_side_effect_count": 0,
            "advisory_only": True,
            "routing_authority": False,
            "policy_promotion_allowed": False,
            "schema_version": PRODUCTION_SHADOW_SCHEMA_VERSION,
            "policy_revision": PRODUCTION_SHADOW_POLICY_REVISION,
        }
        return ProductionShadowDecision(
            decision_id="prodshadow_" + _digest(identity)[:32],
            input_id=shadow_input.input_id,
            input_hash=shadow_input.input_hash,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            selection_rule=policy.selection_rule,
            actual_route_id=shadow_input.actual_route_id,
            actual_route_fingerprint=shadow_input.actual_route_fingerprint,
            state=state,
            selected_route_id=None if selected is None else selected.route_id,
            candidate_order=tuple(item.route_id for item in ordered),
            missing_reason=missing_reason,
            suggestion=suggestion,
            shadow_compute_cost_usd=compute_cost,
            shadow_compute_latency_ms=compute_latency,
            cost_overhead_ratio=round(cost_ratio, 10),
            latency_overhead_ratio=round(latency_ratio, 10),
            cost_overhead_principle_passed=cost_ratio <= MAXIMUM_SHADOW_OVERHEAD_RATIO + 1e-12,
            decision_fingerprint=decision_fingerprint,
        )

    def observe_isolated(
        self,
        shadow_input: ProductionShadowInput,
        candidates: Sequence[ProductionShadowCandidate],
        policy: ProductionShadowPolicy,
        *,
        shadow_compute_cost_usd: float,
        shadow_compute_latency_ms: float,
        observer: Callable[..., ProductionShadowDecision] | None = None,
    ) -> ProductionShadowDecision | ProductionShadowFailureReceipt:
        try:
            return (observer or self.observe)(
                shadow_input,
                candidates,
                policy,
                shadow_compute_cost_usd=shadow_compute_cost_usd,
                shadow_compute_latency_ms=shadow_compute_latency_ms,
            )
        except Exception as exc:  # failure isolation is the purpose of this boundary
            code = "validation_error" if isinstance(exc, ProductionShadowError) else "shadow_internal_error"
            identity = {
                "input_id": shadow_input.input_id,
                "input_hash": shadow_input.input_hash,
                "policy_id": policy.policy_id,
                "policy_version": policy.policy_version,
                "actual_route_id": shadow_input.actual_route_id,
                "actual_route_fingerprint": shadow_input.actual_route_fingerprint,
                "error_code": code,
                "shadow_compute_cost_usd": max(0.0, float(shadow_compute_cost_usd)),
                "shadow_compute_latency_ms": max(0.0, float(shadow_compute_latency_ms)),
                "baseline_execution_continues": True,
                "actual_route_changed": False,
                "error_message_persisted": False,
                "external_call_count": 0,
                "execution_side_effect_count": 0,
                "advisory_only": True,
                "routing_authority": False,
                "schema_version": PRODUCTION_SHADOW_SCHEMA_VERSION,
                "policy_revision": PRODUCTION_SHADOW_POLICY_REVISION,
            }
            return ProductionShadowFailureReceipt(
                failure_id="shadowfail_" + _digest(identity)[:32],
                input_id=shadow_input.input_id,
                input_hash=shadow_input.input_hash,
                policy_id=policy.policy_id,
                policy_version=policy.policy_version,
                actual_route_id=shadow_input.actual_route_id,
                actual_route_fingerprint=shadow_input.actual_route_fingerprint,
                error_code=code,
                shadow_compute_cost_usd=identity["shadow_compute_cost_usd"],
                shadow_compute_latency_ms=identity["shadow_compute_latency_ms"],
            )


@dataclass(frozen=True, slots=True)
class ShadowActualOutcome:
    outcome_id: str
    input_id: str
    actual_route_id: str
    accepted: bool
    actual_total_cost_usd: float
    actual_latency_ms: float
    capacity_admission_correct: bool
    evidence_level: str
    data_origin: str
    independently_validated: bool
    schema_version: str = SHADOW_OUTCOME_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _anonymous(self.outcome_id, "shadowout_", "shadow outcome id")
        _bounded(self.input_id, "shadow_outcome_input_id")
        _bounded(self.actual_route_id, "shadow_outcome_actual_route_id")
        _finite(self.actual_total_cost_usd, "actual_total_cost_usd", minimum=0.0)
        _finite(self.actual_latency_ms, "actual_latency_ms", minimum=0.0)
        if _evidence_rank(self.evidence_level) < 0 or self.data_origin not in ALL_DATA_ORIGINS:
            raise ProductionShadowError("shadow outcome evidence is invalid")
        if self.schema_version != SHADOW_OUTCOME_SCHEMA_VERSION:
            raise ProductionShadowError("shadow outcome schema is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome_id": self.outcome_id,
            "input_id": self.input_id,
            "actual_route_id": self.actual_route_id,
            "accepted": self.accepted,
            "actual_total_cost_usd": self.actual_total_cost_usd,
            "actual_latency_ms": self.actual_latency_ms,
            "capacity_admission_correct": self.capacity_admission_correct,
            "evidence_level": self.evidence_level,
            "data_origin": self.data_origin,
            "independently_validated": self.independently_validated,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ProductionShadowComparison:
    comparison_id: str
    input_id: str
    decision_id: str
    outcome_id: str
    actual_route_id: str
    suggested_route_id: str | None
    same_route: bool | None
    actual_accepted: bool
    baseline_quality_absolute_error: float
    baseline_cost_error_usd: float
    baseline_latency_error_ms: float
    capacity_admission_correct: bool
    counterfactual_outcome_known: bool
    evidence_level: str
    data_origin: str
    independently_validated: bool
    counterfactual_savings_claim_allowed: bool = False
    actual_route_changed: bool = False
    advisory_only: bool = True
    routing_authority: bool = False
    schema_version: str = PRODUCTION_SHADOW_SCHEMA_VERSION
    policy_revision: str = PRODUCTION_SHADOW_POLICY_REVISION

    def __post_init__(self) -> None:
        for value, name in (
            (self.baseline_quality_absolute_error, "baseline_quality_absolute_error"),
            (self.baseline_cost_error_usd, "baseline_cost_error_usd"),
            (self.baseline_latency_error_ms, "baseline_latency_error_ms"),
        ):
            _finite(value, name, minimum=0.0)
        if self.counterfactual_outcome_known != bool(self.same_route):
            raise ProductionShadowError("shadow counterfactual knowledge is inconsistent")
        if self.counterfactual_savings_claim_allowed or self.actual_route_changed or not self.advisory_only or self.routing_authority:
            raise ProductionShadowError("shadow comparison cannot claim or intervene")
        if self.comparison_id != "shadowcmp_" + _digest(self.identity())[:32]:
            raise ProductionShadowError("shadow comparison id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "input_id": self.input_id,
            "decision_id": self.decision_id,
            "outcome_id": self.outcome_id,
            "actual_route_id": self.actual_route_id,
            "suggested_route_id": self.suggested_route_id,
            "same_route": self.same_route,
            "actual_accepted": self.actual_accepted,
            "baseline_quality_absolute_error": self.baseline_quality_absolute_error,
            "baseline_cost_error_usd": self.baseline_cost_error_usd,
            "baseline_latency_error_ms": self.baseline_latency_error_ms,
            "capacity_admission_correct": self.capacity_admission_correct,
            "counterfactual_outcome_known": self.counterfactual_outcome_known,
            "evidence_level": self.evidence_level,
            "data_origin": self.data_origin,
            "independently_validated": self.independently_validated,
            "counterfactual_savings_claim_allowed": self.counterfactual_savings_claim_allowed,
            "actual_route_changed": self.actual_route_changed,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"comparison_id": self.comparison_id, **self.identity()}


def build_shadow_comparison(
    shadow_input: ProductionShadowInput,
    decision: ProductionShadowDecision,
    outcome: ShadowActualOutcome,
) -> ProductionShadowComparison:
    if decision.input_id != shadow_input.input_id or decision.input_hash != shadow_input.input_hash:
        raise ProductionShadowError("shadow decision does not match its input")
    if outcome.input_id != shadow_input.input_id or outcome.actual_route_id != shadow_input.actual_route_id:
        raise ProductionShadowError("shadow outcome does not match the actual route")
    suggested = decision.selected_route_id
    same_route = None if suggested is None else suggested == outcome.actual_route_id
    identity = {
        "input_id": shadow_input.input_id,
        "decision_id": decision.decision_id,
        "outcome_id": outcome.outcome_id,
        "actual_route_id": outcome.actual_route_id,
        "suggested_route_id": suggested,
        "same_route": same_route,
        "actual_accepted": outcome.accepted,
        "baseline_quality_absolute_error": round(abs(shadow_input.baseline_predicted_quality - float(outcome.accepted)), 10),
        "baseline_cost_error_usd": round(abs(shadow_input.baseline_predicted_total_cost_usd - outcome.actual_total_cost_usd), 10),
        "baseline_latency_error_ms": round(abs(shadow_input.baseline_predicted_latency_p95_ms - outcome.actual_latency_ms), 10),
        "capacity_admission_correct": outcome.capacity_admission_correct,
        "counterfactual_outcome_known": bool(same_route),
        "evidence_level": outcome.evidence_level,
        "data_origin": outcome.data_origin,
        "independently_validated": outcome.independently_validated,
        "counterfactual_savings_claim_allowed": False,
        "actual_route_changed": False,
        "advisory_only": True,
        "routing_authority": False,
        "schema_version": PRODUCTION_SHADOW_SCHEMA_VERSION,
        "policy_revision": PRODUCTION_SHADOW_POLICY_REVISION,
    }
    return ProductionShadowComparison(
        comparison_id="shadowcmp_" + _digest(identity)[:32],
        input_id=shadow_input.input_id,
        decision_id=decision.decision_id,
        outcome_id=outcome.outcome_id,
        actual_route_id=outcome.actual_route_id,
        suggested_route_id=suggested,
        same_route=same_route,
        actual_accepted=outcome.accepted,
        baseline_quality_absolute_error=identity["baseline_quality_absolute_error"],
        baseline_cost_error_usd=identity["baseline_cost_error_usd"],
        baseline_latency_error_ms=identity["baseline_latency_error_ms"],
        capacity_admission_correct=outcome.capacity_admission_correct,
        counterfactual_outcome_known=bool(same_route),
        evidence_level=outcome.evidence_level,
        data_origin=outcome.data_origin,
        independently_validated=outcome.independently_validated,
    )


@dataclass(frozen=True, slots=True)
class ProductionShadowAuditReport:
    report_id: str
    batch_hash: str
    input_ids: tuple[str, ...]
    decision_ids: tuple[str, ...]
    failure_ids: tuple[str, ...]
    comparison_ids: tuple[str, ...]
    task_count: int
    advice_count: int
    missing_count: int
    failure_count: int
    comparison_count: int
    coverage_ratio: float
    comparison_coverage_ratio: float
    missing_reason_counts: Mapping[str, int]
    shadow_latency_p50_ms: float
    shadow_latency_p95_ms: float
    cost_overhead_ratio_p50: float
    cost_overhead_ratio_p95: float
    latency_overhead_ratio_p95: float
    counterfactual_unknown_count: int
    checks: Mapping[str, bool]
    state: str
    directional_evidence_eligible: bool
    cost_overhead_principle_passed: bool
    baseline_execution_continuity_proven: bool = True
    execution_side_effect_count: int = 0
    actual_route_change_count: int = 0
    external_call_count: int = 0
    advisory_only: bool = True
    routing_authority: bool = False
    policy_promotion_allowed: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = PRODUCTION_SHADOW_SCHEMA_VERSION
    policy_revision: str = PRODUCTION_SHADOW_POLICY_REVISION

    def __post_init__(self) -> None:
        _sha256(self.batch_hash, "shadow_batch_hash")
        if not 1 <= self.task_count <= MAXIMUM_SHADOW_BATCH:
            raise ProductionShadowError("shadow audit task count is invalid")
        for values, name in (
            (self.input_ids, "shadow audit input ids"),
            (self.decision_ids, "shadow audit decision ids"),
            (self.failure_ids, "shadow audit failure ids"),
            (self.comparison_ids, "shadow audit comparison ids"),
        ):
            if len(values) != len(set(values)):
                raise ProductionShadowError(f"{name} must be unique")
        if (
            len(self.input_ids) != self.task_count
            or len(self.decision_ids) != self.advice_count + self.missing_count
            or len(self.failure_ids) != self.failure_count
            or len(self.comparison_ids) != self.comparison_count
        ):
            raise ProductionShadowError("shadow audit evidence ids contradict counts")
        if self.task_count != self.advice_count + self.missing_count + self.failure_count:
            raise ProductionShadowError("shadow audit state counts are inconsistent")
        if not 0 <= self.comparison_count <= self.task_count:
            raise ProductionShadowError("shadow comparison count is invalid")
        _finite(self.coverage_ratio, "shadow_coverage_ratio", minimum=0.0, maximum=1.0)
        _finite(self.comparison_coverage_ratio, "shadow_comparison_coverage_ratio", minimum=0.0, maximum=1.0)
        if not math.isclose(self.coverage_ratio, self.advice_count / self.task_count, abs_tol=1e-9):
            raise ProductionShadowError("shadow coverage ratio is inconsistent")
        if not math.isclose(self.comparison_coverage_ratio, self.comparison_count / self.task_count, abs_tol=1e-9):
            raise ProductionShadowError("shadow comparison coverage is inconsistent")
        required_checks = {
            "same_inputs_as_baseline",
            "zero_execution_side_effects",
            "all_failures_isolated",
            "minimum_directional_cases",
            "real_e2_outcomes_only",
            "independent_validation",
            "complete_actual_comparisons",
        }
        if set(self.checks) != required_checks:
            raise ProductionShadowError("shadow audit checks are incomplete")
        directional = all(self.checks.values())
        if self.directional_evidence_eligible != directional:
            raise ProductionShadowError("shadow directional evidence gate is inconsistent")
        expected_state = "blocked_evidence" if not directional else (
            "shadow_directional" if self.cost_overhead_principle_passed else "shadow_overhead_exceeded"
        )
        if self.state != expected_state:
            raise ProductionShadowError("shadow audit state contradicts its checks")
        if self.cost_overhead_principle_passed != (self.cost_overhead_ratio_p95 <= MAXIMUM_SHADOW_OVERHEAD_RATIO + 1e-12):
            raise ProductionShadowError("shadow overhead principle is inconsistent")
        if (
            not self.baseline_execution_continuity_proven
            or self.execution_side_effect_count != 0
            or self.actual_route_change_count != 0
            or self.external_call_count != 0
            or not self.advisory_only
            or self.routing_authority
            or self.policy_promotion_allowed
            or self.commercial_claims_allowed
        ):
            raise ProductionShadowError("shadow audit cannot affect production")
        if self.report_id != "shadowaudit_" + _digest(self.identity())[:32]:
            raise ProductionShadowError("shadow audit id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "batch_hash": self.batch_hash,
            "input_ids": list(self.input_ids),
            "decision_ids": list(self.decision_ids),
            "failure_ids": list(self.failure_ids),
            "comparison_ids": list(self.comparison_ids),
            "task_count": self.task_count,
            "advice_count": self.advice_count,
            "missing_count": self.missing_count,
            "failure_count": self.failure_count,
            "comparison_count": self.comparison_count,
            "coverage_ratio": self.coverage_ratio,
            "comparison_coverage_ratio": self.comparison_coverage_ratio,
            "missing_reason_counts": dict(sorted(self.missing_reason_counts.items())),
            "shadow_latency_p50_ms": self.shadow_latency_p50_ms,
            "shadow_latency_p95_ms": self.shadow_latency_p95_ms,
            "cost_overhead_ratio_p50": self.cost_overhead_ratio_p50,
            "cost_overhead_ratio_p95": self.cost_overhead_ratio_p95,
            "latency_overhead_ratio_p95": self.latency_overhead_ratio_p95,
            "counterfactual_unknown_count": self.counterfactual_unknown_count,
            "checks": dict(sorted(self.checks.items())),
            "state": self.state,
            "directional_evidence_eligible": self.directional_evidence_eligible,
            "cost_overhead_principle_passed": self.cost_overhead_principle_passed,
            "baseline_execution_continuity_proven": self.baseline_execution_continuity_proven,
            "execution_side_effect_count": self.execution_side_effect_count,
            "actual_route_change_count": self.actual_route_change_count,
            "external_call_count": self.external_call_count,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "policy_promotion_allowed": self.policy_promotion_allowed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"report_id": self.report_id, **self.identity()}


def build_production_shadow_audit(
    inputs: Sequence[ProductionShadowInput],
    decisions: Sequence[ProductionShadowDecision],
    failures: Sequence[ProductionShadowFailureReceipt],
    comparisons: Sequence[ProductionShadowComparison],
) -> ProductionShadowAuditReport:
    input_items = tuple(sorted(inputs, key=lambda item: item.input_id))
    decision_items = tuple(sorted(decisions, key=lambda item: item.input_id))
    failure_items = tuple(sorted(failures, key=lambda item: item.input_id))
    comparison_items = tuple(sorted(comparisons, key=lambda item: item.input_id))
    if not 1 <= len(input_items) <= MAXIMUM_SHADOW_BATCH:
        raise ProductionShadowError("shadow audit batch size is invalid")
    input_ids = {item.input_id for item in input_items}
    if len(input_ids) != len(input_items):
        raise ProductionShadowError("shadow audit inputs must be unique")
    result_ids = [item.input_id for item in decision_items] + [item.input_id for item in failure_items]
    if len(result_ids) != len(set(result_ids)) or set(result_ids) != input_ids:
        raise ProductionShadowError("every shadow input requires exactly one decision or failure")
    if len({item.input_id for item in comparison_items}) != len(comparison_items):
        raise ProductionShadowError("shadow comparisons must be unique by input")
    decision_ids = {item.decision_id for item in decision_items}
    if any(item.input_id not in input_ids or item.decision_id not in decision_ids for item in comparison_items):
        raise ProductionShadowError("shadow comparison is not linked to this batch")

    advice = sum(item.state == "advice_generated" for item in decision_items)
    missing = sum(item.state == "advice_missing" for item in decision_items)
    reasons: dict[str, int] = {}
    for item in decision_items:
        if item.missing_reason:
            reasons[item.missing_reason] = reasons.get(item.missing_reason, 0) + 1
    for item in failure_items:
        reasons[item.error_code] = reasons.get(item.error_code, 0) + 1
    latencies = [item.shadow_compute_latency_ms for item in decision_items] + [item.shadow_compute_latency_ms for item in failure_items]
    cost_ratios = [item.cost_overhead_ratio for item in decision_items]
    input_by_id = {item.input_id: item for item in input_items}
    for item in failure_items:
        baseline = input_by_id[item.input_id]
        cost_ratios.append(item.shadow_compute_cost_usd / baseline.baseline_end_to_end_estimated_cost_usd)
    latency_ratios = [item.latency_overhead_ratio for item in decision_items]
    for item in failure_items:
        baseline = input_by_id[item.input_id]
        latency_ratios.append(item.shadow_compute_latency_ms / baseline.baseline_end_to_end_estimated_latency_ms)
    all_same_inputs = all(
        item.same_features_as_baseline and item.same_catalog_as_baseline and item.same_capacity_as_baseline
        for item in input_items
    )
    real_e2 = bool(comparison_items) and all(
        _evidence_rank(item.evidence_level) >= 2 and item.data_origin in REAL_DATA_ORIGINS
        for item in comparison_items
    )
    independent = bool(comparison_items) and all(item.independently_validated for item in comparison_items)
    checks = {
        "same_inputs_as_baseline": all_same_inputs,
        "zero_execution_side_effects": all(item.execution_side_effect_count == 0 and not item.actual_route_changed for item in decision_items) and all(item.execution_side_effect_count == 0 and not item.actual_route_changed for item in failure_items),
        "all_failures_isolated": all(item.baseline_execution_continues for item in failure_items),
        "minimum_directional_cases": len(comparison_items) >= MINIMUM_DIRECTIONAL_SHADOW_CASES,
        "real_e2_outcomes_only": real_e2,
        "independent_validation": independent,
        "complete_actual_comparisons": len(comparison_items) == len(input_items),
    }
    directional = all(checks.values())
    cost_p50 = statistics.median(cost_ratios)
    cost_p95 = _percentile(cost_ratios, 0.95)
    overhead_passed = cost_p95 <= MAXIMUM_SHADOW_OVERHEAD_RATIO + 1e-12
    state = "blocked_evidence" if not directional else (
        "shadow_directional" if overhead_passed else "shadow_overhead_exceeded"
    )
    batch_hash = _digest(
        {
            "inputs": [item.to_dict() for item in input_items],
            "decisions": [item.to_dict() for item in decision_items],
            "failures": [item.to_dict() for item in failure_items],
            "comparisons": [item.to_dict() for item in comparison_items],
        }
    )
    identity = {
        "batch_hash": batch_hash,
        "input_ids": [item.input_id for item in input_items],
        "decision_ids": [item.decision_id for item in decision_items],
        "failure_ids": [item.failure_id for item in failure_items],
        "comparison_ids": [item.comparison_id for item in comparison_items],
        "task_count": len(input_items),
        "advice_count": advice,
        "missing_count": missing,
        "failure_count": len(failure_items),
        "comparison_count": len(comparison_items),
        "coverage_ratio": round(advice / len(input_items), 10),
        "comparison_coverage_ratio": round(len(comparison_items) / len(input_items), 10),
        "missing_reason_counts": dict(sorted(reasons.items())),
        "shadow_latency_p50_ms": round(statistics.median(latencies), 10),
        "shadow_latency_p95_ms": round(_percentile(latencies, 0.95), 10),
        "cost_overhead_ratio_p50": round(cost_p50, 10),
        "cost_overhead_ratio_p95": round(cost_p95, 10),
        "latency_overhead_ratio_p95": round(_percentile(latency_ratios, 0.95), 10),
        "counterfactual_unknown_count": sum(not item.counterfactual_outcome_known for item in comparison_items),
        "checks": dict(sorted(checks.items())),
        "state": state,
        "directional_evidence_eligible": directional,
        "cost_overhead_principle_passed": overhead_passed,
        "baseline_execution_continuity_proven": True,
        "execution_side_effect_count": 0,
        "actual_route_change_count": 0,
        "external_call_count": 0,
        "advisory_only": True,
        "routing_authority": False,
        "policy_promotion_allowed": False,
        "commercial_claims_allowed": False,
        "schema_version": PRODUCTION_SHADOW_SCHEMA_VERSION,
        "policy_revision": PRODUCTION_SHADOW_POLICY_REVISION,
    }
    return ProductionShadowAuditReport(
        report_id="shadowaudit_" + _digest(identity)[:32],
        batch_hash=batch_hash,
        input_ids=tuple(item.input_id for item in input_items),
        decision_ids=tuple(item.decision_id for item in decision_items),
        failure_ids=tuple(item.failure_id for item in failure_items),
        comparison_ids=tuple(item.comparison_id for item in comparison_items),
        task_count=len(input_items),
        advice_count=advice,
        missing_count=missing,
        failure_count=len(failure_items),
        comparison_count=len(comparison_items),
        coverage_ratio=identity["coverage_ratio"],
        comparison_coverage_ratio=identity["comparison_coverage_ratio"],
        missing_reason_counts=reasons,
        shadow_latency_p50_ms=identity["shadow_latency_p50_ms"],
        shadow_latency_p95_ms=identity["shadow_latency_p95_ms"],
        cost_overhead_ratio_p50=identity["cost_overhead_ratio_p50"],
        cost_overhead_ratio_p95=identity["cost_overhead_ratio_p95"],
        latency_overhead_ratio_p95=identity["latency_overhead_ratio_p95"],
        counterfactual_unknown_count=identity["counterfactual_unknown_count"],
        checks=checks,
        state=state,
        directional_evidence_eligible=directional,
        cost_overhead_principle_passed=overhead_passed,
    )


def public_production_shadow_policy() -> dict[str, Any]:
    return {
        "schema_version": PRODUCTION_SHADOW_SCHEMA_VERSION,
        "policy_revision": PRODUCTION_SHADOW_POLICY_REVISION,
        "same_task_features_required": True,
        "same_catalog_snapshot_required": True,
        "same_capacity_snapshot_required": True,
        "maximum_capacity_age_ms": MAXIMUM_CAPACITY_AGE_MS,
        "selection_rules": sorted(SELECTION_RULES),
        "missing_reasons": sorted(MISSING_REASONS),
        "failure_isolation": {
            "baseline_execution_continues": True,
            "error_message_persisted": False,
            "execution_side_effect_count": 0,
        },
        "overhead": {
            "maximum_end_to_end_cost_ratio": MAXIMUM_SHADOW_OVERHEAD_RATIO,
            "p95_gate": True,
            "current_threshold_assessment_allowed": False,
        },
        "evidence_gate": {
            "minimum_directional_cases": MINIMUM_DIRECTIONAL_SHADOW_CASES,
            "real_e2_outcomes_required": True,
            "independent_validation_required": True,
            "complete_actual_comparisons_required": True,
            "current_evidence": "E0-contract-only",
        },
        "counterfactual_outcome_default": "unknown",
        "external_calls_required": False,
        "actual_route_changed": False,
        "advisory_only": True,
        "routing_authority": False,
        "policy_promotion_allowed": False,
        "fixture_metrics_are_claimable": False,
        "commercial_claims_allowed": False,
    }


__all__ = [
    "MAXIMUM_SHADOW_OVERHEAD_RATIO",
    "MINIMUM_DIRECTIONAL_SHADOW_CASES",
    "PRODUCTION_SHADOW_POLICY_REVISION",
    "PRODUCTION_SHADOW_SCHEMA_VERSION",
    "ProductionLikeShadowEngine",
    "ProductionShadowAuditReport",
    "ProductionShadowCandidate",
    "ProductionShadowComparison",
    "ProductionShadowDecision",
    "ProductionShadowError",
    "ProductionShadowFailureReceipt",
    "ProductionShadowInput",
    "ProductionShadowPolicy",
    "ShadowActualOutcome",
    "build_production_shadow_audit",
    "build_shadow_comparison",
    "public_production_shadow_policy",
]
