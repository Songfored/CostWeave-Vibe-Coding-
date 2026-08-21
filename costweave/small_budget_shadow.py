"""Small-budget, advisory-only Shadow Runtime for v0.6.6 V066-WP07.

The runtime deliberately owns admission and budget reservation, while the
provider transport is injected.  The default installation has no executor and
therefore cannot make a network call.  Only bounded metrics (and optionally an
output digest) are persisted; prompts, raw outputs, and secret values are not.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping, Protocol, runtime_checkable

from .adapters.http import AdapterRuntimePolicy
from .contracts_v4 import utc_now
from .evaluation_protocol import canonical_json
from .experimental_calls import (
    ExperimentBudget,
    ExperimentalCallStore,
    ExternalAttempt,
    ExternalBudgetExceededError,
    ExternalIdempotencyError,
)
from .planning_contracts import PlanningContractError, WorkOrder
from .security_boundary import (
    DEFAULT_DATA_RESIDENCY,
    DEFAULT_PROJECT_ID,
    PromptInjectionError,
    SecretLeaseError,
    SecretReference,
    assess_prompt_security,
    contains_secret_material,
    normalize_data_residency,
    normalize_project_id,
    require_prompt_admission,
)


SMALL_BUDGET_SHADOW_SCHEMA_VERSION = "v0.6.6"
SMALL_BUDGET_SHADOW_POLICY_REVISION = "small-budget-shadow-v1"
SHADOW_DATA_SCOPES = ("metrics_only", "metrics_and_output_hash")
SHADOW_DEPLOYMENTS = ("local", "remote")
SHADOW_REASONING_EFFORTS = ("low", "medium", "high", "xhigh")
SHADOW_EVIDENCE_LEVELS = ("E0-fixture", "E2-real-unvalidated")

MAX_SMALL_BUDGET_USD = 5.0
MAX_SHADOW_LOGICAL_CALLS = 20
# WP07 intentionally performs no automatic retry.  A failed or ambiguous call
# may already have consumed provider budget; retrying before WP08 billing
# reconciliation would weaken the hard-budget guarantee.
MAX_SHADOW_ATTEMPTS_PER_PROBE = 1
MAX_SHADOW_OUTPUT_TOKENS = 8_192
MAX_SHADOW_LIFETIME_SECONDS = 86_400

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SmallBudgetShadowError(ValueError):
    """Raised when a WP07 contract or isolation invariant is violated."""


class SmallBudgetShadowNotFoundError(KeyError):
    """Raised for missing or cross-project shadow sessions."""


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _bounded_text(value: Any, name: str, *, maximum: int = 160) -> str:
    if not isinstance(value, str):
        raise SmallBudgetShadowError(f"{name} must be text")
    text = value.strip()
    if not text or len(text) > maximum or "\n" in text or "\r" in text:
        raise SmallBudgetShadowError(f"{name} must be bounded text")
    if contains_secret_material(text):
        raise SmallBudgetShadowError(f"{name} appears to contain secret material")
    return text


def _identifier(value: Any, name: str) -> str:
    text = _bounded_text(value, name)
    if not _IDENTIFIER_RE.fullmatch(text):
        raise SmallBudgetShadowError(f"{name} contains unsupported characters")
    return text


def _finite(
    value: Any,
    name: str,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise SmallBudgetShadowError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise SmallBudgetShadowError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number < minimum:
        raise SmallBudgetShadowError(f"{name} is outside its allowed range")
    if maximum is not None and number > maximum:
        raise SmallBudgetShadowError(f"{name} exceeds its maximum")
    return number


def _integer(
    value: Any,
    name: str,
    *,
    minimum: int = 1,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SmallBudgetShadowError(f"{name} must be an integer")
    if not minimum <= value <= maximum:
        raise SmallBudgetShadowError(f"{name} is outside its allowed range")
    return value


def _boolean(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise SmallBudgetShadowError(f"{name} must be bool")
    return value


def _timestamp(value: Any, name: str) -> tuple[str, datetime]:
    text = _bounded_text(value, name, maximum=80)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SmallBudgetShadowError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise SmallBudgetShadowError(f"{name} must include timezone")
    return text, parsed.astimezone(UTC)


def _sha256(value: Any, name: str) -> str:
    text = str(value).lower()
    if not _SHA256_RE.fullmatch(text):
        raise SmallBudgetShadowError(f"{name} must be a sha256 digest")
    return text


def _strict_json(value: Any, name: str, *, maximum_bytes: int = 64_000) -> Any:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SmallBudgetShadowError(f"{name} must be finite JSON") from exc
    if len(raw.encode("utf-8")) > maximum_bytes:
        raise SmallBudgetShadowError(f"{name} is too large")
    return json.loads(raw)


def _work_order(value: Any) -> WorkOrder:
    if not isinstance(value, Mapping):
        raise SmallBudgetShadowError("work_order must be an object")
    allowed = {
        "work_order_id",
        "task_id",
        "objective",
        "dependencies",
        "input_artifacts",
        "context_manifest",
        "assumptions",
        "allowed_tools",
        "forbidden_actions",
        "output_schema",
        "acceptance_tests",
        "cost_limit",
        "time_limit_ms",
        "escalation_conditions",
        "idempotency_key",
        "schema_version",
    }
    unexpected = set(value) - allowed
    if unexpected:
        raise SmallBudgetShadowError(
            "work_order contains unsupported fields: "
            + ", ".join(sorted(str(item) for item in unexpected))
        )
    required = allowed - {"schema_version"}
    missing = required - set(value)
    if missing:
        raise SmallBudgetShadowError(
            "work_order is missing fields: "
            + ", ".join(sorted(missing))
        )
    sequence_fields = (
        "dependencies",
        "input_artifacts",
        "context_manifest",
        "assumptions",
        "allowed_tools",
        "forbidden_actions",
        "acceptance_tests",
        "escalation_conditions",
    )
    if any(
        not isinstance(value[name], (list, tuple))
        for name in sequence_fields
    ):
        raise SmallBudgetShadowError(
            "work_order sequence fields must be arrays"
        )
    if any(
        not isinstance(item, Mapping)
        for item in value["context_manifest"]
    ):
        raise SmallBudgetShadowError(
            "work_order context_manifest items must be objects"
        )
    if not isinstance(value["output_schema"], Mapping):
        raise SmallBudgetShadowError("work_order output_schema must be an object")
    try:
        return WorkOrder(
            work_order_id=str(value["work_order_id"]),
            task_id=str(value["task_id"]),
            objective=str(value["objective"]),
            dependencies=tuple(value["dependencies"]),
            input_artifacts=tuple(value["input_artifacts"]),
            context_manifest=tuple(
                dict(item) for item in value["context_manifest"]
            ),
            assumptions=tuple(str(item) for item in value["assumptions"]),
            allowed_tools=tuple(str(item) for item in value["allowed_tools"]),
            forbidden_actions=tuple(
                str(item) for item in value["forbidden_actions"]
            ),
            output_schema=dict(value["output_schema"]),
            acceptance_tests=tuple(
                str(item) for item in value["acceptance_tests"]
            ),
            cost_limit=_finite(
                value["cost_limit"],
                "work_order.cost_limit",
                maximum=MAX_SMALL_BUDGET_USD,
            ),
            time_limit_ms=_integer(
                value["time_limit_ms"],
                "work_order.time_limit_ms",
                maximum=MAX_SHADOW_LIFETIME_SECONDS * 1_000,
            ),
            escalation_conditions=tuple(
                str(item) for item in value["escalation_conditions"]
            ),
            idempotency_key=str(value["idempotency_key"]),
            schema_version=str(value.get("schema_version", "v0.5.1")),
        )
    except (PlanningContractError, TypeError, ValueError) as exc:
        raise SmallBudgetShadowError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class ShadowExperimentConfigV1:
    idempotency_key: str
    provider: str
    model_id: str
    deployment: str
    data_scope: str
    hard_budget_usd: float
    provider_budget_usd: float
    max_cost_per_probe_usd: float
    max_logical_calls: int
    max_attempts_per_probe: int
    max_output_tokens_per_probe: int
    expires_at: str
    project_id: str = DEFAULT_PROJECT_ID
    data_residency: str = DEFAULT_DATA_RESIDENCY
    secret_env: str | None = None
    reasoning_effort: str = "medium"
    live_calls_requested: bool = False
    network_consent: bool = False
    paid_consent: bool = False
    shadow_only_acknowledged: bool = True
    schema_version: str = SMALL_BUDGET_SHADOW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SMALL_BUDGET_SHADOW_SCHEMA_VERSION:
            raise SmallBudgetShadowError("shadow configuration schema is invalid")
        _identifier(self.idempotency_key, "idempotency_key")
        _identifier(self.provider, "provider")
        _bounded_text(self.model_id, "model_id")
        if self.deployment not in SHADOW_DEPLOYMENTS:
            raise SmallBudgetShadowError("deployment must be local or remote")
        if self.data_scope not in SHADOW_DATA_SCOPES:
            raise SmallBudgetShadowError("data_scope is not supported")
        if self.reasoning_effort not in SHADOW_REASONING_EFFORTS:
            raise SmallBudgetShadowError("reasoning_effort is not supported")
        if normalize_project_id(self.project_id) != self.project_id:
            raise SmallBudgetShadowError("project_id is not canonical")
        if normalize_data_residency(self.data_residency) != self.data_residency:
            raise SmallBudgetShadowError("data_residency is not canonical")
        total = _finite(
            self.hard_budget_usd,
            "hard_budget_usd",
            minimum=0.000001,
            maximum=MAX_SMALL_BUDGET_USD,
        )
        provider = _finite(
            self.provider_budget_usd,
            "provider_budget_usd",
            minimum=0.000001,
            maximum=total,
        )
        _finite(
            self.max_cost_per_probe_usd,
            "max_cost_per_probe_usd",
            minimum=0.000001,
            maximum=provider,
        )
        _integer(
            self.max_logical_calls,
            "max_logical_calls",
            maximum=MAX_SHADOW_LOGICAL_CALLS,
        )
        _integer(
            self.max_attempts_per_probe,
            "max_attempts_per_probe",
            maximum=MAX_SHADOW_ATTEMPTS_PER_PROBE,
        )
        _integer(
            self.max_output_tokens_per_probe,
            "max_output_tokens_per_probe",
            maximum=MAX_SHADOW_OUTPUT_TOKENS,
        )
        expires_text, expires = _timestamp(self.expires_at, "expires_at")
        now = datetime.now(UTC)
        if expires <= now or expires > now + timedelta(
            seconds=MAX_SHADOW_LIFETIME_SECONDS
        ):
            raise SmallBudgetShadowError(
                "expires_at must be in the next 24 hours"
            )
        if expires_text != self.expires_at:
            raise SmallBudgetShadowError("expires_at is not canonical")
        for value, name in (
            (self.live_calls_requested, "live_calls_requested"),
            (self.network_consent, "network_consent"),
            (self.paid_consent, "paid_consent"),
            (self.shadow_only_acknowledged, "shadow_only_acknowledged"),
        ):
            _boolean(value, name)
        if not self.shadow_only_acknowledged:
            raise SmallBudgetShadowError(
                "advisory-only behavior must be explicitly acknowledged"
            )
        if self.live_calls_requested and not self.network_consent:
            raise SmallBudgetShadowError(
                "live shadow calls require explicit network consent"
            )
        if self.deployment == "remote":
            if not self.secret_env:
                raise SmallBudgetShadowError(
                    "remote shadow provider requires a secret reference"
                )
            if not self.paid_consent:
                raise SmallBudgetShadowError(
                    "remote shadow provider requires explicit paid-call consent"
                )
        if self.secret_env is not None:
            try:
                SecretReference(self.secret_env)
            except SecretLeaseError as exc:
                raise SmallBudgetShadowError(str(exc)) from exc

    @property
    def experiment_id(self) -> str:
        return "experiment_" + _hash(
            {
                "project_id": self.project_id,
                "idempotency_key": self.idempotency_key,
                "purpose": SMALL_BUDGET_SHADOW_POLICY_REVISION,
            }
        )[:32]

    @property
    def deployment_id(self) -> str:
        return "deployment_" + _hash(
            {
                "provider": self.provider,
                "model_id": self.model_id,
                "deployment": self.deployment,
                "reasoning_effort": self.reasoning_effort,
            }
        )[:24]

    def fingerprint_payload(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "deployment": self.deployment,
            "data_scope": self.data_scope,
            "hard_budget_usd": self.hard_budget_usd,
            "provider_budget_usd": self.provider_budget_usd,
            "max_cost_per_probe_usd": self.max_cost_per_probe_usd,
            "max_logical_calls": self.max_logical_calls,
            "max_attempts_per_probe": self.max_attempts_per_probe,
            "max_output_tokens_per_probe": self.max_output_tokens_per_probe,
            "expires_at": self.expires_at,
            "project_id": self.project_id,
            "data_residency": self.data_residency,
            "secret_configured": self.secret_env is not None,
            "reasoning_effort": self.reasoning_effort,
            "live_calls_requested": self.live_calls_requested,
            "network_consent": self.network_consent,
            "paid_consent": self.paid_consent,
            "shadow_only_acknowledged": self.shadow_only_acknowledged,
            "schema_version": self.schema_version,
        }

    @property
    def config_fingerprint(self) -> str:
        # A secret reference is not public, but it is part of idempotency.
        # Hashing it prevents the same key from silently switching credential
        # sources without persisting or returning the environment name.
        return _hash(
            {
                **self.fingerprint_payload(),
                "secret_reference_fingerprint": (
                    _hash(
                        {
                            "purpose": "wp07-secret-reference",
                            "secret_env": self.secret_env,
                        }
                    )
                    if self.secret_env is not None
                    else None
                ),
            }
        )

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "experiment_id": self.experiment_id,
            "deployment_id": self.deployment_id,
            **self.fingerprint_payload(),
            "config_fingerprint": self.config_fingerprint,
            "advisory_only": True,
            "routing_authority": False,
            "actual_route_changed": False,
            "secrets_persisted": False,
        }

    def to_budget(self) -> ExperimentBudget:
        return ExperimentBudget(
            experiment_id=self.experiment_id,
            total_cost_limit_usd=self.hard_budget_usd,
            provider_cost_limits_usd={
                self.provider: self.provider_budget_usd
            },
            max_logical_calls=self.max_logical_calls,
            max_attempts_per_call=self.max_attempts_per_probe,
            purpose=(
                "small_budget_shadow:" + self.config_fingerprint
            ),
            expires_at=self.expires_at,
            revision=SMALL_BUDGET_SHADOW_POLICY_REVISION,
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShadowExperimentConfigV1":
        allowed = {
            "idempotency_key",
            "provider",
            "model_id",
            "deployment",
            "data_scope",
            "hard_budget_usd",
            "provider_budget_usd",
            "max_cost_per_probe_usd",
            "max_logical_calls",
            "max_attempts_per_probe",
            "max_output_tokens_per_probe",
            "expires_at",
            "project_id",
            "data_residency",
            "secret_env",
            "reasoning_effort",
            "live_calls_requested",
            "network_consent",
            "paid_consent",
            "shadow_only_acknowledged",
            "schema_version",
        }
        unexpected = set(value) - allowed
        if unexpected:
            raise SmallBudgetShadowError(
                "shadow configuration contains unsupported fields: "
                + ", ".join(sorted(str(item) for item in unexpected))
            )
        required = {
            "idempotency_key",
            "provider",
            "model_id",
            "deployment",
            "data_scope",
            "hard_budget_usd",
            "provider_budget_usd",
            "max_cost_per_probe_usd",
            "max_logical_calls",
            "max_attempts_per_probe",
            "max_output_tokens_per_probe",
            "expires_at",
        }
        missing = required - set(value)
        if missing:
            raise SmallBudgetShadowError(
                "shadow configuration is missing fields: "
                + ", ".join(sorted(missing))
            )
        return cls(
            idempotency_key=str(value["idempotency_key"]),
            provider=str(value["provider"]),
            model_id=str(value["model_id"]),
            deployment=str(value["deployment"]),
            data_scope=str(value["data_scope"]),
            hard_budget_usd=value["hard_budget_usd"],
            provider_budget_usd=value["provider_budget_usd"],
            max_cost_per_probe_usd=value["max_cost_per_probe_usd"],
            max_logical_calls=value["max_logical_calls"],
            max_attempts_per_probe=value["max_attempts_per_probe"],
            max_output_tokens_per_probe=value["max_output_tokens_per_probe"],
            expires_at=str(value["expires_at"]),
            project_id=normalize_project_id(
                value.get("project_id", DEFAULT_PROJECT_ID)
            ),
            data_residency=normalize_data_residency(
                value.get("data_residency", DEFAULT_DATA_RESIDENCY)
            ),
            secret_env=(
                str(value["secret_env"])
                if value.get("secret_env") is not None
                else None
            ),
            reasoning_effort=str(value.get("reasoning_effort", "medium")),
            live_calls_requested=value.get("live_calls_requested", False),
            network_consent=value.get("network_consent", False),
            paid_consent=value.get("paid_consent", False),
            shadow_only_acknowledged=value.get(
                "shadow_only_acknowledged", True
            ),
            schema_version=str(
                value.get(
                    "schema_version", SMALL_BUDGET_SHADOW_SCHEMA_VERSION
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ShadowSessionV1:
    config: ShadowExperimentConfigV1
    armed: bool
    block_reason: str | None
    replayed: bool
    configured_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.config.to_public_dict(),
            "armed": self.armed,
            "block_reason": self.block_reason,
            "replayed": self.replayed,
            "configured_at": self.configured_at,
        }


@dataclass(frozen=True, slots=True)
class ShadowProbeRequestV1:
    experiment_id: str
    project_id: str
    work_order: WorkOrder
    baseline_route_id: str
    baseline_route_fingerprint: str
    max_output_tokens: int
    idempotency_key: str
    security_approval_id: str | None = None
    schema_version: str = SMALL_BUDGET_SHADOW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SMALL_BUDGET_SHADOW_SCHEMA_VERSION:
            raise SmallBudgetShadowError("shadow probe schema is invalid")
        if not self.experiment_id.startswith("experiment_"):
            raise SmallBudgetShadowError("shadow experiment id is invalid")
        if normalize_project_id(self.project_id) != self.project_id:
            raise SmallBudgetShadowError("project_id is not canonical")
        _identifier(self.baseline_route_id, "baseline_route_id")
        _sha256(
            self.baseline_route_fingerprint,
            "baseline_route_fingerprint",
        )
        _identifier(self.idempotency_key, "idempotency_key")
        _integer(
            self.max_output_tokens,
            "max_output_tokens",
            maximum=MAX_SHADOW_OUTPUT_TOKENS,
        )
        if self.security_approval_id is not None:
            _identifier(
                self.security_approval_id, "security_approval_id"
            )
        if self.work_order.allowed_tools:
            raise SmallBudgetShadowError(
                "WP07 shadow probes are text-only and tool-free"
            )
        if self.work_order.task_id.strip() == "":
            raise SmallBudgetShadowError("shadow task attribution is required")
        _identifier(self.work_order.work_order_id, "work_order_id")
        _identifier(self.work_order.task_id, "task_id")
        _identifier(
            self.work_order.idempotency_key,
            "work_order.idempotency_key",
        )
        _strict_json(self.work_order.to_dict(), "work_order")
        if contains_secret_material(self.work_order.to_dict()):
            raise SmallBudgetShadowError(
                "secret material is never admitted to a shadow probe"
            )

    @property
    def fingerprint(self) -> str:
        return _hash(
            {
                "experiment_id": self.experiment_id,
                "project_id": self.project_id,
                "work_order_hash": _hash(self.work_order.to_dict()),
                "baseline_route_id": self.baseline_route_id,
                "baseline_route_fingerprint": self.baseline_route_fingerprint,
                "max_output_tokens": self.max_output_tokens,
                "security_approval_present": self.security_approval_id
                is not None,
                "schema_version": self.schema_version,
            }
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShadowProbeRequestV1":
        allowed = {
            "experiment_id",
            "project_id",
            "work_order",
            "baseline_route_id",
            "baseline_route_fingerprint",
            "max_output_tokens",
            "idempotency_key",
            "security_approval_id",
            "schema_version",
        }
        unexpected = set(value) - allowed
        if unexpected:
            raise SmallBudgetShadowError(
                "shadow probe contains unsupported fields: "
                + ", ".join(sorted(str(item) for item in unexpected))
            )
        missing = {
            "experiment_id",
            "project_id",
            "work_order",
            "baseline_route_id",
            "baseline_route_fingerprint",
            "max_output_tokens",
            "idempotency_key",
        } - set(value)
        if missing:
            raise SmallBudgetShadowError(
                "shadow probe is missing fields: "
                + ", ".join(sorted(missing))
            )
        return cls(
            experiment_id=str(value["experiment_id"]),
            project_id=normalize_project_id(value["project_id"]),
            work_order=_work_order(value["work_order"]),
            baseline_route_id=str(value["baseline_route_id"]),
            baseline_route_fingerprint=str(
                value["baseline_route_fingerprint"]
            ),
            max_output_tokens=value["max_output_tokens"],
            idempotency_key=str(value["idempotency_key"]),
            security_approval_id=(
                str(value["security_approval_id"])
                if value.get("security_approval_id") is not None
                else None
            ),
            schema_version=str(
                value.get(
                    "schema_version", SMALL_BUDGET_SHADOW_SCHEMA_VERSION
                )
            ),
        )


@dataclass(frozen=True, slots=True)
class ShadowProbeMeasurementV1:
    accepted: bool
    actual_cost_usd: float
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int
    latency_ms: int
    output_hash: str | None
    evidence_level: str = "E2-real-unvalidated"

    def __post_init__(self) -> None:
        _boolean(self.accepted, "accepted")
        _finite(
            self.actual_cost_usd,
            "actual_cost_usd",
            maximum=MAX_SMALL_BUDGET_USD,
        )
        for value, name in (
            (self.input_tokens, "input_tokens"),
            (self.output_tokens, "output_tokens"),
            (self.cached_input_tokens, "cached_input_tokens"),
            (self.latency_ms, "latency_ms"),
        ):
            _integer(value, name, minimum=0, maximum=1_000_000_000)
        if self.output_hash is not None:
            _sha256(self.output_hash, "output_hash")
        _bounded_text(self.evidence_level, "evidence_level")
        if self.evidence_level not in SHADOW_EVIDENCE_LEVELS:
            raise SmallBudgetShadowError(
                "shadow evidence level is not admitted by WP07"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "actual_cost_usd": self.actual_cost_usd,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "latency_ms": self.latency_ms,
            "output_hash": self.output_hash,
            "evidence_level": self.evidence_level,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ShadowProbeMeasurementV1":
        allowed = {
            "accepted",
            "actual_cost_usd",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "latency_ms",
            "output_hash",
            "evidence_level",
        }
        unexpected = set(value) - allowed
        missing = {
            "accepted",
            "actual_cost_usd",
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "latency_ms",
        } - set(value)
        if unexpected or missing:
            raise SmallBudgetShadowError(
                "persisted shadow measurement violates its schema"
            )
        return cls(
            accepted=value["accepted"],
            actual_cost_usd=value["actual_cost_usd"],
            input_tokens=value["input_tokens"],
            output_tokens=value["output_tokens"],
            cached_input_tokens=value["cached_input_tokens"],
            latency_ms=value["latency_ms"],
            output_hash=value.get("output_hash"),
            evidence_level=str(
                value.get("evidence_level", "E2-real-unvalidated")
            ),
        )


@runtime_checkable
class ShadowProbeExecutor(Protocol):
    def execute(
        self,
        config: ShadowExperimentConfigV1,
        request: ShadowProbeRequestV1,
    ) -> ShadowProbeMeasurementV1:
        ...


@dataclass(frozen=True, slots=True)
class ShadowProbeReceiptV1:
    receipt_id: str
    experiment_id: str
    external_call_id: str | None
    request_fingerprint: str
    status: str
    reason: str | None
    measurement: ShadowProbeMeasurementV1 | None
    replayed: bool
    external_call_count: int
    recorded_at: str
    baseline_execution_continues: bool = True
    actual_route_changed: bool = False
    routing_authority: bool = False
    policy_promotion_allowed: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = SMALL_BUDGET_SHADOW_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SMALL_BUDGET_SHADOW_SCHEMA_VERSION:
            raise SmallBudgetShadowError("shadow receipt schema is invalid")
        _identifier(self.receipt_id, "receipt_id")
        if not self.experiment_id.startswith("experiment_"):
            raise SmallBudgetShadowError("shadow receipt experiment id is invalid")
        if self.external_call_id is not None:
            _identifier(self.external_call_id, "external_call_id")
        _timestamp(self.recorded_at, "recorded_at")
        _boolean(self.replayed, "replayed")
        for value, name in (
            (self.baseline_execution_continues, "baseline_execution_continues"),
            (self.actual_route_changed, "actual_route_changed"),
            (self.routing_authority, "routing_authority"),
            (self.policy_promotion_allowed, "policy_promotion_allowed"),
            (self.commercial_claims_allowed, "commercial_claims_allowed"),
        ):
            _boolean(value, name)
        if self.status not in {"completed", "failed", "blocked"}:
            raise SmallBudgetShadowError("shadow probe receipt status is invalid")
        if self.measurement is not None and not isinstance(
            self.measurement, ShadowProbeMeasurementV1
        ):
            raise SmallBudgetShadowError(
                "shadow probe receipt measurement is invalid"
            )
        if (self.status == "completed") != (self.measurement is not None):
            raise SmallBudgetShadowError("shadow probe measurement state is inconsistent")
        if self.status == "completed" and self.reason is not None:
            raise SmallBudgetShadowError("completed shadow probe cannot have a reason")
        if self.status != "completed" and not self.reason:
            raise SmallBudgetShadowError("non-completed shadow probe needs a reason")
        if self.reason is not None:
            _bounded_text(self.reason, "reason")
        _sha256(self.request_fingerprint, "request_fingerprint")
        _integer(
            self.external_call_count,
            "external_call_count",
            minimum=0,
            maximum=1,
        )
        if (
            not self.baseline_execution_continues
            or self.actual_route_changed
            or self.routing_authority
            or self.policy_promotion_allowed
            or self.commercial_claims_allowed
        ):
            raise SmallBudgetShadowError(
                "shadow receipt cannot affect authoritative execution"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "receipt_id": self.receipt_id,
            "experiment_id": self.experiment_id,
            "external_call_id": self.external_call_id,
            "request_fingerprint": self.request_fingerprint,
            "status": self.status,
            "reason": self.reason,
            "measurement": (
                None if self.measurement is None else self.measurement.to_dict()
            ),
            "replayed": self.replayed,
            "external_call_count": self.external_call_count,
            "recorded_at": self.recorded_at,
            "baseline_execution_continues": self.baseline_execution_continues,
            "actual_route_changed": self.actual_route_changed,
            "routing_authority": self.routing_authority,
            "policy_promotion_allowed": self.policy_promotion_allowed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
        }


class SmallBudgetShadowRuntime:
    """Project-scoped WP07 admission and budget owner."""

    def __init__(
        self,
        store: ExperimentalCallStore,
        *,
        executor: ShadowProbeExecutor | None = None,
        adapter_policy: AdapterRuntimePolicy | None = None,
    ) -> None:
        self.store = store
        self.executor = executor
        self.adapter_policy = adapter_policy or AdapterRuntimePolicy.from_environment()
        self._configs: dict[str, ShadowExperimentConfigV1] = {}
        self._configured_at: dict[str, str] = {}
        self._lock = threading.RLock()

    def _admission(self, config: ShadowExperimentConfigV1) -> tuple[bool, str | None]:
        if not config.live_calls_requested:
            return False, "live_calls_not_requested"
        if not self.adapter_policy.experimental_calls_enabled:
            return False, "experimental_calls_disabled"
        if config.deployment == "remote" and not self.adapter_policy.paid_calls_enabled:
            return False, "paid_calls_disabled"
        if (
            config.deployment == "remote"
            and (
                not config.secret_env
                or not os.environ.get(config.secret_env, "").strip()
            )
        ):
            return False, "secret_unavailable"
        if self.executor is None:
            return False, "shadow_executor_unavailable"
        return True, None

    def configure(self, config: ShadowExperimentConfigV1) -> ShadowSessionV1:
        if not isinstance(config, ShadowExperimentConfigV1):
            if isinstance(config, Mapping):
                config = ShadowExperimentConfigV1.from_dict(config)
            else:
                raise SmallBudgetShadowError(
                    "configure requires ShadowExperimentConfigV1"
                )
        with self._lock:
            existing = self._configs.get(config.experiment_id)
            if (
                existing is not None
                and existing.config_fingerprint != config.config_fingerprint
            ):
                raise ExternalIdempotencyError(
                    "shadow configuration key was reused for different content"
                )
            created = self.store.register_experiment(config.to_budget())
            if existing is None:
                self._configs[config.experiment_id] = config
                self._configured_at[config.experiment_id] = utc_now()
            armed, reason = self._admission(config)
            return ShadowSessionV1(
                config=config,
                armed=armed,
                block_reason=reason,
                replayed=not created,
                configured_at=self._configured_at[config.experiment_id],
            )

    def session(self, experiment_id: str, *, project_id: str) -> ShadowSessionV1:
        project = normalize_project_id(project_id)
        with self._lock:
            config = self._configs.get(str(experiment_id))
            if config is None or config.project_id != project:
                raise SmallBudgetShadowNotFoundError(experiment_id)
            armed, reason = self._admission(config)
            return ShadowSessionV1(
                config=config,
                armed=armed,
                block_reason=reason,
                replayed=False,
                configured_at=self._configured_at[config.experiment_id],
            )

    def summary(self, experiment_id: str, *, project_id: str) -> dict[str, Any]:
        session = self.session(experiment_id, project_id=project_id)
        return {
            "session": session.to_dict(),
            "budget": self.store.experiment_summary(experiment_id),
        }

    def counts(self) -> dict[str, int]:
        with self._lock:
            configs = tuple(self._configs.values())
        store_counts = self.store.counts()
        return {
            "configured_sessions": len(configs),
            "armed_sessions": sum(self._admission(item)[0] for item in configs),
            "external_calls": int(store_counts.get("external_calls", 0)),
            "external_attempts": int(
                store_counts.get("external_call_attempts", 0)
            ),
            "route_interventions": 0,
            "production_promotions": 0,
        }

    def _receipt(
        self,
        request: ShadowProbeRequestV1,
        *,
        status: str,
        reason: str | None,
        measurement: ShadowProbeMeasurementV1 | None,
        replayed: bool,
        external_call_id: str | None,
        external_call_count: int,
    ) -> ShadowProbeReceiptV1:
        identity = {
            "experiment_id": request.experiment_id,
            "request_fingerprint": request.fingerprint,
            "status": status,
            "reason": reason,
            "measurement": (
                None if measurement is None else measurement.to_dict()
            ),
            "external_call_id": external_call_id,
        }
        return ShadowProbeReceiptV1(
            receipt_id="shadowprobe_" + _hash(identity)[:32],
            experiment_id=request.experiment_id,
            external_call_id=external_call_id,
            request_fingerprint=request.fingerprint,
            status=status,
            reason=reason,
            measurement=measurement,
            replayed=replayed,
            external_call_count=external_call_count,
            recorded_at=utc_now(),
        )

    def probe(self, request: ShadowProbeRequestV1) -> ShadowProbeReceiptV1:
        if not isinstance(request, ShadowProbeRequestV1):
            if isinstance(request, Mapping):
                request = ShadowProbeRequestV1.from_dict(request)
            else:
                raise SmallBudgetShadowError(
                    "probe requires ShadowProbeRequestV1"
                )
        session = self.session(
            request.experiment_id, project_id=request.project_id
        )
        config = session.config
        if request.max_output_tokens > config.max_output_tokens_per_probe:
            raise SmallBudgetShadowError(
                "probe output limit exceeds the configured ceiling"
            )
        if request.work_order.cost_limit + 1e-10 < config.max_cost_per_probe_usd:
            raise SmallBudgetShadowError(
                "WorkOrder cost limit cannot fund the configured probe ceiling"
            )
        assessment = assess_prompt_security(
            request.work_order.objective,
            *request.work_order.assumptions,
            *(
                canonical_json(item)
                for item in request.work_order.context_manifest
            ),
            *request.work_order.acceptance_tests,
        )
        try:
            require_prompt_admission(
                assessment,
                approval_id=request.security_approval_id,
            )
        except PromptInjectionError as exc:
            raise SmallBudgetShadowError(str(exc)) from exc
        if not session.armed:
            return self._receipt(
                request,
                status="blocked",
                reason=session.block_reason,
                measurement=None,
                replayed=False,
                external_call_id=None,
                external_call_count=0,
            )

        try:
            reservation = self.store.reserve(
                experiment_id=config.experiment_id,
                run_id=request.work_order.work_order_id,
                task_id=request.work_order.task_id,
                deployment_id=config.deployment_id,
                provider=config.provider,
                model_id=config.model_id,
                adapter_revision=SMALL_BUDGET_SHADOW_POLICY_REVISION,
                reasoning_effort=config.reasoning_effort,
                idempotency_key=request.idempotency_key,
                request_fingerprint={
                    "shadow_request_fingerprint": request.fingerprint,
                    "config_fingerprint": config.config_fingerprint,
                    "data_scope": config.data_scope,
                },
                worst_case_cost_per_attempt_usd=config.max_cost_per_probe_usd,
                max_attempts=config.max_attempts_per_probe,
            )
        except ExternalBudgetExceededError:
            return self._receipt(
                request,
                status="blocked",
                reason="hard_budget_exceeded",
                measurement=None,
                replayed=False,
                external_call_id=None,
                external_call_count=0,
            )

        if reservation.replayed:
            if reservation.status == "completed" and reservation.result:
                measurement = ShadowProbeMeasurementV1.from_dict(
                    reservation.result
                )
                return self._receipt(
                    request,
                    status="completed",
                    reason=None,
                    measurement=measurement,
                    replayed=True,
                    external_call_id=reservation.external_call_id,
                    external_call_count=0,
                )
            return self._receipt(
                request,
                status=(
                    "failed"
                    if reservation.status in {"failed", "cancelled"}
                    else "blocked"
                ),
                reason=(reservation.error_code or "probe_in_progress"),
                measurement=None,
                replayed=True,
                external_call_id=reservation.external_call_id,
                external_call_count=0,
            )

        assert self.executor is not None
        measurement: ShadowProbeMeasurementV1 | None = None
        try:
            measurement = self.executor.execute(config, request)
            if not isinstance(measurement, ShadowProbeMeasurementV1):
                raise SmallBudgetShadowError(
                    "shadow executor returned an unsupported measurement"
                )
            if (
                config.data_scope == "metrics_only"
                and measurement.output_hash is not None
            ):
                raise SmallBudgetShadowError(
                    "metrics_only scope cannot retain an output hash"
                )
            if (
                config.data_scope == "metrics_and_output_hash"
                and measurement.output_hash is None
            ):
                raise SmallBudgetShadowError(
                    "output-hash scope requires an output hash"
                )
            if measurement.output_tokens > request.max_output_tokens:
                raise SmallBudgetShadowError(
                    "provider output exceeded the admitted token ceiling"
                )
            self.store.record_attempt(
                reservation.external_call_id,
                ExternalAttempt(
                    ordinal=1,
                    status="completed",
                    input_tokens=measurement.input_tokens,
                    output_tokens=measurement.output_tokens,
                    cached_input_tokens=measurement.cached_input_tokens,
                    actual_cost_usd=measurement.actual_cost_usd,
                    request_latency_ms=measurement.latency_ms,
                    response_hash=measurement.output_hash,
                ),
            )
            self.store.finalize(
                reservation.external_call_id,
                status="completed",
                result=measurement.to_dict(),
                telemetry={
                    "data_scope": config.data_scope,
                    "baseline_execution_continues": True,
                    "actual_route_changed": False,
                    "routing_authority": False,
                    "raw_prompt_persisted": False,
                    "raw_output_persisted": False,
                    "secret_value_persisted": False,
                },
            )
            return self._receipt(
                request,
                status="completed",
                reason=None,
                measurement=measurement,
                replayed=False,
                external_call_id=reservation.external_call_id,
                external_call_count=1,
            )
        except ExternalIdempotencyError:
            raise
        except Exception as exc:
            error_code = (
                "actual_cost_exceeded_reservation"
                if isinstance(exc, ExternalBudgetExceededError)
                else "shadow_executor_failed"
            )
            # An executor error can happen after the Provider accepted a paid
            # request.  Until WP08 reconciles invoices, conservatively consume
            # the admitted worst case instead of releasing the reservation and
            # creating a path to silent overspend.  When a valid measurement
            # was returned, retain its observed cost.
            conservative_cost = (
                measurement.actual_cost_usd
                if isinstance(measurement, ShadowProbeMeasurementV1)
                else config.max_cost_per_probe_usd
            )
            try:
                self.store.record_attempt(
                    reservation.external_call_id,
                    ExternalAttempt(
                        ordinal=1,
                        status="failed",
                        actual_cost_usd=conservative_cost,
                        error_code=error_code,
                    ),
                )
                self.store.finalize(
                    reservation.external_call_id,
                    status="failed",
                    result=None,
                    telemetry={
                        "data_scope": config.data_scope,
                        "baseline_execution_continues": True,
                        "actual_route_changed": False,
                        "raw_prompt_persisted": False,
                        "raw_output_persisted": False,
                        "secret_value_persisted": False,
                    },
                    error_code=error_code,
                )
            except Exception:
                # The durable budget owner may already have frozen the call;
                # never replace the original failure with cleanup noise.
                pass
            return self._receipt(
                request,
                status="failed",
                reason=error_code,
                measurement=None,
                replayed=False,
                external_call_id=reservation.external_call_id,
                external_call_count=1,
            )


def public_small_budget_shadow_policy() -> dict[str, Any]:
    return {
        "schema_version": SMALL_BUDGET_SHADOW_SCHEMA_VERSION,
        "policy_revision": SMALL_BUDGET_SHADOW_POLICY_REVISION,
        "advisory_only": True,
        "routing_authority": False,
        "actual_route_changed": False,
        "baseline_execution_continues": True,
        "live_calls_enabled_by_default": False,
        "paid_calls_enabled_by_default": False,
        "executor_available_by_default": False,
        "explicit_provider_required": True,
        "explicit_secret_reference_required_for_remote": True,
        "explicit_data_scope_required": True,
        "hard_budget_required": True,
        "atomic_budget_reservation": True,
        "secrets_persisted": False,
        "raw_prompts_persisted": False,
        "raw_outputs_persisted": False,
        "data_scopes": list(SHADOW_DATA_SCOPES),
        "evidence_levels": list(SHADOW_EVIDENCE_LEVELS),
        "limits": {
            "maximum_total_budget_usd": MAX_SMALL_BUDGET_USD,
            "maximum_logical_calls": MAX_SHADOW_LOGICAL_CALLS,
            "maximum_attempts_per_probe": MAX_SHADOW_ATTEMPTS_PER_PROBE,
            "maximum_output_tokens_per_probe": MAX_SHADOW_OUTPUT_TOKENS,
            "maximum_lifetime_seconds": MAX_SHADOW_LIFETIME_SECONDS,
        },
        "project_scoped": True,
        "idempotent": True,
        "failure_isolated": True,
        "policy_promotion_allowed": False,
        "commercial_claims_allowed": False,
        "current_evidence": "E0-contract-and-fixture-only",
    }


__all__ = [
    "MAX_SHADOW_ATTEMPTS_PER_PROBE",
    "MAX_SHADOW_LIFETIME_SECONDS",
    "MAX_SHADOW_LOGICAL_CALLS",
    "MAX_SHADOW_OUTPUT_TOKENS",
    "MAX_SMALL_BUDGET_USD",
    "SHADOW_DATA_SCOPES",
    "SHADOW_EVIDENCE_LEVELS",
    "SMALL_BUDGET_SHADOW_POLICY_REVISION",
    "SMALL_BUDGET_SHADOW_SCHEMA_VERSION",
    "ShadowExperimentConfigV1",
    "ShadowProbeExecutor",
    "ShadowProbeMeasurementV1",
    "ShadowProbeReceiptV1",
    "ShadowProbeRequestV1",
    "ShadowSessionV1",
    "SmallBudgetShadowError",
    "SmallBudgetShadowNotFoundError",
    "SmallBudgetShadowRuntime",
    "public_small_budget_shadow_policy",
]
