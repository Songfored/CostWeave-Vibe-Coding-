"""Capacity-aware local Ollama/vLLM OpenAI-compatible gateway (V066-WP04).

The local endpoint still speaks the provider-neutral OpenAI-compatible contract
implemented by WP02.  This module adds a separate, fail-closed admission layer:
the caller must explicitly enable local network access, capacity admission and
an injected telemetry probe before a provider request can be attempted.

No discovery socket, metrics SDK or provider credential is created here.  A
real Ollama/vLLM metrics reader can implement :class:`LocalCapacityProbe`; the
in-process static probe is only an E0 contract fixture.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol

from .gateway_backend import (
    GATEWAY_SCHEMA_VERSION,
    GatewayBackendDisabledError,
    GatewayCapabilitiesV1,
    GatewayRequestV1,
    GatewayStreamEventV1,
)
from .gateway_openai import (
    GatewayHTTPTransport,
    OpenAICompatibleGatewayBackend,
    OpenAICompatibleGatewayConfig,
    OpenAICompatibleGatewayError,
    OpenAICompatibleRuntimePolicy,
)
from .intelligence.common import bounded_text, validate_timestamp


LOCAL_ENDPOINT_REVISION = "local-openai-capacity-v1"
LOCAL_ENDPOINT_POLICY_REVISION = "local-endpoint-admission-v1"
LOCAL_ENDPOINT_KINDS = ("generic", "ollama", "vllm")
_MAX_QUEUE_VALUE = 1_000_000
_MAX_WAIT_MS = 86_400_000
_MAX_AGE_MS = 86_400_000
_MAX_CLOCK_SKEW_MS = 5_000


def _identifier(value: Any, name: str) -> str:
    try:
        return bounded_text(value, name, minimum=1, maximum=192)
    except (TypeError, ValueError) as exc:
        raise OpenAICompatibleGatewayError(str(exc)) from exc


def _optional_non_negative(value: Any, name: str, maximum: int) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > maximum:
        raise OpenAICompatibleGatewayError(f"{name} must be a bounded non-negative int or null")
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _captured_at_age_ms(captured_at: str) -> int:
    candidate = captured_at[:-1] + "+00:00" if captured_at.endswith(("Z", "z")) else captured_at
    captured = datetime.fromisoformat(candidate).astimezone(timezone.utc)
    return int((datetime.now(timezone.utc) - captured).total_seconds() * 1000)


@dataclass(frozen=True, slots=True)
class LocalEndpointConfig:
    """Normalized configuration for an Ollama/vLLM-style local endpoint."""

    backend_id: str
    endpoint: str
    endpoint_kind: str = "generic"
    timeout_seconds: float = 30.0
    max_response_bytes: int = 4_000_000
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend_id", _identifier(self.backend_id, "backend_id"))
        if self.endpoint_kind not in LOCAL_ENDPOINT_KINDS:
            raise OpenAICompatibleGatewayError("unsupported local endpoint kind")
        # Reuse WP02's strict loopback, no-credential, no-query validation.
        OpenAICompatibleGatewayConfig(
            backend_id=self.backend_id,
            endpoint=self.endpoint,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
            schema_version=self.schema_version,
        )

    def to_openai_config(self) -> OpenAICompatibleGatewayConfig:
        return OpenAICompatibleGatewayConfig(
            backend_id=self.backend_id,
            endpoint=self.endpoint,
            timeout_seconds=self.timeout_seconds,
            max_response_bytes=self.max_response_bytes,
            schema_version=self.schema_version,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.to_openai_config().to_dict(),
            "endpoint_kind": self.endpoint_kind,
            "gateway_revision": LOCAL_ENDPOINT_REVISION,
        }


@dataclass(frozen=True, slots=True)
class LocalCapacitySnapshot:
    """Normalized, bounded capacity telemetry read immediately before a call.

    ``None`` means the source did not provide that field.  A snapshot is only
    complete when all fields, including freshness, are present.  This makes
    missing metrics an explicit admission failure rather than an optimistic
    zero-load assumption.
    """

    snapshot_id: str
    source: str
    queue_depth: int | None = None
    queue_limit: int | None = None
    available_slots: int | None = None
    estimated_wait_ms: int | None = None
    telemetry_age_ms: int | None = None
    captured_at: str = ""
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "snapshot_id", _identifier(self.snapshot_id, "snapshot_id"))
        object.__setattr__(self, "source", _identifier(self.source, "source"))
        for name, maximum in (
            ("queue_depth", _MAX_QUEUE_VALUE),
            ("queue_limit", _MAX_QUEUE_VALUE),
            ("available_slots", _MAX_QUEUE_VALUE),
            ("estimated_wait_ms", _MAX_WAIT_MS),
            ("telemetry_age_ms", _MAX_AGE_MS),
        ):
            object.__setattr__(self, name, _optional_non_negative(getattr(self, name), name, maximum))
        if self.queue_depth is not None and self.queue_limit is not None and self.queue_depth > self.queue_limit:
            raise OpenAICompatibleGatewayError("queue_depth cannot exceed queue_limit")
        captured = self.captured_at or _now()
        try:
            captured = validate_timestamp(captured, "captured_at")
        except (TypeError, ValueError) as exc:
            raise OpenAICompatibleGatewayError(str(exc)) from exc
        object.__setattr__(self, "captured_at", captured)
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise OpenAICompatibleGatewayError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    @property
    def complete(self) -> bool:
        return all(
            value is not None
            for value in (
                self.queue_depth,
                self.queue_limit,
                self.available_slots,
                self.estimated_wait_ms,
                self.telemetry_age_ms,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "queue_depth": self.queue_depth,
            "queue_limit": self.queue_limit,
            "available_slots": self.available_slots,
            "estimated_wait_ms": self.estimated_wait_ms,
            "telemetry_age_ms": self.telemetry_age_ms,
            "captured_at": self.captured_at,
            "complete": self.complete,
            "schema_version": self.schema_version,
        }


class LocalCapacityProbe(Protocol):
    """Caller-owned normalized capacity reader for a local endpoint."""

    def read(self, *, backend_id: str, model: str) -> LocalCapacitySnapshot | None:
        ...


class StaticLocalCapacityProbe:
    """Deterministic E0 probe used by tests and offline examples."""

    evidence_level = "E0-static-capacity"

    def __init__(self, snapshot: LocalCapacitySnapshot | None = None) -> None:
        self.snapshot = snapshot
        self.calls: list[dict[str, str]] = []

    def read(self, *, backend_id: str, model: str) -> LocalCapacitySnapshot | None:
        self.calls.append({"backend_id": backend_id, "model": model})
        return self.snapshot


@dataclass(frozen=True, slots=True)
class LocalEndpointAdmissionPolicy:
    """Hard limits for local capacity admission.

    Defaults intentionally reject the call path until a caller explicitly
    enables the policy and chooses tolerances appropriate to its hardware.
    """

    capacity_admission_enabled: bool = False
    require_complete_telemetry: bool = True
    max_telemetry_age_ms: int = 30_000
    min_available_slots: int = 1
    max_queue_depth: int = 0
    max_estimated_wait_ms: int = 0
    policy_revision: str = LOCAL_ENDPOINT_POLICY_REVISION

    def __post_init__(self) -> None:
        if not isinstance(self.capacity_admission_enabled, bool) or not isinstance(self.require_complete_telemetry, bool):
            raise OpenAICompatibleGatewayError("local capacity policy flags must be bool")
        if not self.require_complete_telemetry:
            raise OpenAICompatibleGatewayError(
                "require_complete_telemetry cannot be disabled for local capacity admission"
            )
        for name, value, maximum in (
            ("max_telemetry_age_ms", self.max_telemetry_age_ms, _MAX_AGE_MS),
            ("min_available_slots", self.min_available_slots, _MAX_QUEUE_VALUE),
            ("max_queue_depth", self.max_queue_depth, _MAX_QUEUE_VALUE),
            ("max_estimated_wait_ms", self.max_estimated_wait_ms, _MAX_WAIT_MS),
        ):
            _optional_non_negative(value, name, maximum)
        if self.policy_revision != LOCAL_ENDPOINT_POLICY_REVISION:
            raise OpenAICompatibleGatewayError(
                f"policy_revision must be {LOCAL_ENDPOINT_POLICY_REVISION}"
            )

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": GATEWAY_SCHEMA_VERSION,
            "policy_revision": self.policy_revision,
            "capacity_admission_enabled": self.capacity_admission_enabled,
            "require_complete_telemetry": self.require_complete_telemetry,
            "max_telemetry_age_ms": self.max_telemetry_age_ms,
            "min_available_slots": self.min_available_slots,
            "max_queue_depth": self.max_queue_depth,
            "max_estimated_wait_ms": self.max_estimated_wait_ms,
            "missing_telemetry_fails_closed": True,
            "endpoint_kinds": list(LOCAL_ENDPOINT_KINDS),
        }


@dataclass(frozen=True, slots=True)
class LocalCapacityDecision:
    request_id: str
    admitted: bool
    reason_code: str
    reasons: tuple[str, ...]
    snapshot: Mapping[str, Any] | None
    policy_revision: str = LOCAL_ENDPOINT_POLICY_REVISION

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "admitted": self.admitted,
            "reason_code": self.reason_code,
            "reasons": list(self.reasons),
            "snapshot": None if self.snapshot is None else dict(self.snapshot),
            "policy_revision": self.policy_revision,
        }


class LocalEndpointAdmissionError(GatewayBackendDisabledError):
    """Raised when local capacity cannot be proven safe for this request."""

    def __init__(self, message: str, *, decision: LocalCapacityDecision) -> None:
        super().__init__(message)
        self.decision = decision


def _evaluate_capacity(
    request: GatewayRequestV1,
    snapshot: LocalCapacitySnapshot | None,
    policy: LocalEndpointAdmissionPolicy,
) -> LocalCapacityDecision:
    if not policy.capacity_admission_enabled:
        return LocalCapacityDecision(
            request_id=request.request_id,
            admitted=False,
            reason_code="capacity_admission_disabled",
            reasons=("capacity_admission_disabled",),
            snapshot=None if snapshot is None else snapshot.to_dict(),
        )
    if snapshot is None:
        return LocalCapacityDecision(
            request_id=request.request_id,
            admitted=False,
            reason_code="telemetry_missing",
            reasons=("telemetry_missing",),
            snapshot=None,
        )
    reasons: list[str] = []
    snapshot_payload = snapshot.to_dict()
    if policy.require_complete_telemetry and not snapshot.complete:
        reasons.append("telemetry_incomplete")
    captured_at_age_ms = _captured_at_age_ms(snapshot.captured_at)
    if snapshot.telemetry_age_ms is None:
        reasons.append("telemetry_age_missing")
    else:
        effective_age_ms = max(snapshot.telemetry_age_ms, captured_at_age_ms, 0)
        if captured_at_age_ms < -_MAX_CLOCK_SKEW_MS:
            reasons.append("telemetry_from_future")
        elif effective_age_ms > policy.max_telemetry_age_ms:
            reasons.append("telemetry_stale")
    if snapshot.available_slots is None:
        reasons.append("available_slots_missing")
    elif snapshot.available_slots < policy.min_available_slots:
        reasons.append("insufficient_slots")
    if snapshot.queue_depth is None:
        reasons.append("queue_depth_missing")
    elif snapshot.queue_depth > policy.max_queue_depth:
        reasons.append("queue_depth_exceeded")
    if snapshot.estimated_wait_ms is None:
        reasons.append("estimated_wait_missing")
    elif snapshot.estimated_wait_ms > policy.max_estimated_wait_ms:
        reasons.append("estimated_wait_exceeded")
    if reasons:
        return LocalCapacityDecision(
            request_id=request.request_id,
            admitted=False,
            reason_code=reasons[0],
            reasons=tuple(reasons),
            snapshot=snapshot_payload,
        )
    return LocalCapacityDecision(
        request_id=request.request_id,
        admitted=True,
        reason_code="admitted",
        reasons=(),
        snapshot=snapshot_payload,
    )


class LocalOpenAICompatibleGatewayBackend:
    """WP04 capacity-aware wrapper around the WP02 local gateway backend."""

    def __init__(
        self,
        config: LocalEndpointConfig,
        *,
        policy: OpenAICompatibleRuntimePolicy | None = None,
        capacity_policy: LocalEndpointAdmissionPolicy | None = None,
        capacity_probe: LocalCapacityProbe | None = None,
        transport: GatewayHTTPTransport | None = None,
    ) -> None:
        self.config = config
        self.policy = policy or OpenAICompatibleRuntimePolicy()
        self.capacity_policy = capacity_policy or LocalEndpointAdmissionPolicy()
        self.capacity_probe = capacity_probe
        self._backend = OpenAICompatibleGatewayBackend(
            config.to_openai_config(),
            policy=self.policy,
            transport=transport,
        )
        self._last_decision: LocalCapacityDecision | None = None
        self._admission_history: list[LocalCapacityDecision] = []
        self._capabilities = replace(
            self._backend.capabilities,
            backend_id=config.backend_id,
            protocol="local-openai-compatible",
            reports_queue_time=True,
        )

    @property
    def capabilities(self) -> GatewayCapabilitiesV1:
        return self._capabilities

    @property
    def last_admission(self) -> LocalCapacityDecision | None:
        return self._last_decision

    @property
    def admission_history(self) -> tuple[LocalCapacityDecision, ...]:
        return tuple(self._admission_history)

    def _record(self, decision: LocalCapacityDecision) -> None:
        self._last_decision = decision
        self._admission_history.append(decision)
        del self._admission_history[:-256]

    def _admit_capacity(self, request: GatewayRequestV1) -> LocalCapacityDecision:
        if not self.policy.network_enabled:
            decision = LocalCapacityDecision(
                request_id=request.request_id,
                admitted=False,
                reason_code="network_disabled",
                reasons=("network_disabled",),
                snapshot=None,
            )
            self._record(decision)
            raise LocalEndpointAdmissionError(
                "local endpoint is disabled by network policy", decision=decision
            )
        snapshot = None
        probe_failed = False
        if self.capacity_probe is not None:
            try:
                snapshot = self.capacity_probe.read(
                    backend_id=self.config.backend_id,
                    model=request.model,
                )
            except Exception:
                # Cross the untrusted probe boundary without retaining its
                # exception as __context__ or __cause__; logging a caller's
                # traceback must not disclose probe internals or credentials.
                probe_failed = True
        if probe_failed:
            decision = LocalCapacityDecision(
                request_id=request.request_id,
                admitted=False,
                reason_code="telemetry_probe_failed",
                reasons=("telemetry_probe_failed",),
                snapshot=None,
            )
            self._record(decision)
            raise LocalEndpointAdmissionError(
                "local capacity telemetry is unavailable", decision=decision
            )
        if self.capacity_probe is not None:
            if snapshot is not None and not isinstance(snapshot, LocalCapacitySnapshot):
                decision = LocalCapacityDecision(
                    request_id=request.request_id,
                    admitted=False,
                    reason_code="telemetry_invalid",
                    reasons=("telemetry_invalid",),
                    snapshot=None,
                )
                self._record(decision)
                raise LocalEndpointAdmissionError(
                    "local capacity telemetry is invalid", decision=decision
                )
        decision = _evaluate_capacity(request, snapshot, self.capacity_policy)
        self._record(decision)
        if not decision.admitted:
            raise LocalEndpointAdmissionError(
                "local endpoint capacity admission failed", decision=decision
            )
        return decision

    def submit(self, request: GatewayRequestV1):
        self._admit_capacity(request)
        return self._backend.submit(request)

    def stream(self, request: GatewayRequestV1) -> Iterable[GatewayStreamEventV1]:
        self._admit_capacity(request)
        yield from self._backend.stream(request)

    def cancel(self, request_id: str, *, reason: str):
        return self._backend.cancel(request_id, reason=reason)

    def public(self) -> dict[str, Any]:
        return {
            "gateway_revision": LOCAL_ENDPOINT_REVISION,
            "endpoint_kind": self.config.endpoint_kind,
            "config": self.config.to_dict(),
            "network_policy": self.policy.public(),
            "capacity_policy": self.capacity_policy.public(),
            "capacity_probe_injected": self.capacity_probe is not None,
            "evidence_level": getattr(self.capacity_probe, "evidence_level", "E0-missing"),
        }


def public_local_endpoint_policy() -> dict[str, Any]:
    return {
        "gateway_revision": LOCAL_ENDPOINT_REVISION,
        "policy": LocalEndpointAdmissionPolicy().public(),
        "network_enabled": False,
        "capacity_admission_enabled": False,
        "local_endpoint_only": True,
        "remote_endpoint_allowed": False,
        "missing_telemetry_fails_closed": True,
    }


__all__ = [
    "LOCAL_ENDPOINT_KINDS",
    "LOCAL_ENDPOINT_POLICY_REVISION",
    "LOCAL_ENDPOINT_REVISION",
    "LocalCapacityDecision",
    "LocalCapacityProbe",
    "LocalCapacitySnapshot",
    "LocalEndpointAdmissionError",
    "LocalEndpointAdmissionPolicy",
    "LocalEndpointConfig",
    "LocalOpenAICompatibleGatewayBackend",
    "StaticLocalCapacityProbe",
    "public_local_endpoint_policy",
]
