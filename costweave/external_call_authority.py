"""Single in-process authority for guarded external side effects in v0.6.7."""

from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

from .approvals import ApprovalStore
from .evaluation_protocol import canonical_json
from .experimental_calls import (
    CallReservation,
    ExperimentalCallError,
    ExperimentalCallStore,
    ExternalCallsDisabledError,
)
from .planning_contracts import WorkOrder
from .security_boundary import (
    DEFAULT_ADAPTER_REGISTRY,
    AdapterPluginManifest,
    SecretBroker,
    SecretLease,
    SecretLeaseError,
    SecretReference,
    assess_prompt_security,
    normalize_data_residency,
    normalize_project_id,
    prompt_approval_binding,
    require_prompt_admission,
    validate_endpoint,
)
from .security_store import SecurityStore


EXTERNAL_CALL_AUTHORITY_SCHEMA_VERSION = "v0.6.7"


class ExternalCallAuthorityError(ExperimentalCallError):
    """The caller attempted to bypass or reuse an external-call capability."""


@dataclass(frozen=True, slots=True)
class ExternalCallAdmissionV1:
    secret_lease: SecretLease | None
    prompt_security: Mapping[str, Any]
    manifest: AdapterPluginManifest
    project_id: str
    data_residency: str
    schema_version: str = EXTERNAL_CALL_AUTHORITY_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class ExternalTransportCapabilityV1:
    """Non-serializable, process-local permission for bounded attempts."""

    capability_id: str
    external_call_id: str
    side_effect_id: str
    project_id: str
    data_residency: str
    endpoint: str
    request_hash: str
    provider_idempotency_key: str
    max_attempts: int
    schema_version: str = EXTERNAL_CALL_AUTHORITY_SCHEMA_VERSION

    def public(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "external_call_id": self.external_call_id,
            "side_effect_id": self.side_effect_id,
            "project_id": self.project_id,
            "data_residency": self.data_residency,
            "destination_host": urlparse(self.endpoint).hostname,
            "request_hash": self.request_hash,
            "provider_idempotency_key": self.provider_idempotency_key,
            "max_attempts": self.max_attempts,
            "contains_secret_value": False,
            "serializable_secret_handle": False,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class ExternalAuthorityReservationV1:
    logical: CallReservation
    mode: str
    side_effect_id: str | None = None
    capability: ExternalTransportCapabilityV1 | None = None
    schema_version: str = EXTERNAL_CALL_AUTHORITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.mode not in {
            "transport_authorized",
            "logical_replay",
            "global_side_effect_replay",
        }:
            raise ExternalCallAuthorityError("authority reservation mode is invalid")
        has_capability = self.capability is not None
        if has_capability != (self.mode == "transport_authorized"):
            raise ExternalCallAuthorityError(
                "only a new side effect may carry a transport capability"
            )


class ExternalCallAuthorityV1:
    """Own admission, budget reservation, idempotency and transport reachability."""

    def __init__(
        self,
        store: ExperimentalCallStore,
        *,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.store = store
        self.security_store = SecurityStore(store.path)
        self.approval_store = ApprovalStore(store.path)
        self.secret_broker = secret_broker or SecretBroker()
        self._lock = threading.RLock()
        self._capabilities: dict[str, dict[str, Any]] = {}

    def admit(
        self,
        *,
        plugin_id: str,
        adapter_id: str,
        adapter_revision: str,
        deployment: str,
        endpoint: str,
        secret_env: str | None,
        paid: bool,
        experimental_calls_enabled: bool,
        paid_calls_enabled: bool,
        project_id: str,
        data_residency: str,
        run_id: str,
        task_id: str,
        work_order: WorkOrder,
        security_approval_id: str | None,
    ) -> ExternalCallAdmissionV1:
        if not experimental_calls_enabled:
            raise ExternalCallsDisabledError("experimental adapters are disabled")
        if paid and not paid_calls_enabled:
            raise ExternalCallsDisabledError("paid calls are disabled")
        if work_order.allowed_tools:
            raise ExperimentalCallError(
                "v0.6.7 HTTP adapters are text-only and tool-free"
            )
        if work_order.cost_limit < 0:
            raise ExperimentalCallError("WorkOrder cost limit is invalid")

        project = self.security_store.ensure_project_active(project_id)
        residency = normalize_data_residency(data_residency)
        manifest = DEFAULT_ADAPTER_REGISTRY.require(
            plugin_id,
            adapter_revision=adapter_revision,
            deployment=deployment,
            secret_required=bool(secret_env),
        )
        validate_endpoint(endpoint, deployment=deployment)
        assessment = assess_prompt_security(
            work_order.objective,
            *work_order.assumptions,
        )
        intent_fingerprint = hashlib.sha256(
            canonical_json(work_order.to_dict()).encode("utf-8")
        ).hexdigest()
        approval_kind, approval_reason = prompt_approval_binding(
            assessment,
            project_id=project,
            run_id=run_id,
            task_id=task_id,
            intent_fingerprint=intent_fingerprint,
        )
        require_prompt_admission(
            assessment,
            approval_id=security_approval_id,
            approval_validator=lambda approval_id: self.approval_store.is_approved_for(
                approval_id,
                run_id=run_id,
                task_id=task_id,
                kind=approval_kind,
                reason=approval_reason,
            ),
        )

        secret_lease = None
        if secret_env:
            try:
                secret_lease = self.secret_broker.lease(
                    adapter_id=adapter_id,
                    reference=SecretReference(secret_env),
                )
            except SecretLeaseError as error:
                raise ExternalCallsDisabledError(
                    "adapter secret is unavailable"
                ) from error
        return ExternalCallAdmissionV1(
            secret_lease=secret_lease,
            prompt_security=assessment.to_dict(),
            manifest=manifest,
            project_id=project,
            data_residency=residency,
        )

    def reserve(
        self,
        *,
        logical_values: Mapping[str, Any],
        project_id: str,
        data_residency: str,
        adapter_id: str,
        provider: str,
        deployment_id: str,
        endpoint: str,
        model_id: str,
        adapter_revision: str,
        operation: str,
        provider_idempotency_key: str,
        request_hash: str,
        max_attempts: int,
    ) -> ExternalAuthorityReservationV1:
        logical = self.store.reserve(**dict(logical_values))
        if logical.replayed:
            return ExternalAuthorityReservationV1(
                logical=logical,
                mode="logical_replay",
            )

        authority_nonce = secrets.token_urlsafe(48)
        try:
            side_effect, replayed = self.security_store.reserve_side_effect(
                project_id=project_id,
                data_residency=data_residency,
                adapter_id=adapter_id,
                provider=provider,
                deployment_id=deployment_id,
                endpoint=endpoint,
                model_id=model_id,
                adapter_revision=adapter_revision,
                operation=operation,
                idempotency_key=provider_idempotency_key,
                request_hash=request_hash,
                provider_idempotency_key=provider_idempotency_key,
                compensation_strategy="not_required_idempotent",
                origin_external_call_id=logical.external_call_id,
                authority_nonce=authority_nonce,
            )
        except Exception:
            self.store.finalize(
                logical.external_call_id,
                status="failed",
                result=None,
                telemetry={
                    "transport_called": False,
                    "actual_cost_usd": 0.0,
                    "external_call_authority": EXTERNAL_CALL_AUTHORITY_SCHEMA_VERSION,
                },
                error_code="side_effect_authorization_failed",
            )
            raise
        side_effect_id = str(side_effect["side_effect_id"])
        if replayed:
            return ExternalAuthorityReservationV1(
                logical=logical,
                mode="global_side_effect_replay",
                side_effect_id=side_effect_id,
            )

        capability = ExternalTransportCapabilityV1(
            capability_id="external_cap_" + secrets.token_hex(24),
            external_call_id=logical.external_call_id,
            side_effect_id=side_effect_id,
            project_id=normalize_project_id(project_id),
            data_residency=normalize_data_residency(data_residency),
            endpoint=endpoint,
            request_hash=request_hash,
            provider_idempotency_key=provider_idempotency_key,
            max_attempts=int(max_attempts),
        )
        with self._lock:
            self._capabilities[capability.capability_id] = {
                "endpoint": endpoint,
                "request_hash": request_hash,
                "provider_idempotency_key": provider_idempotency_key,
                "authority_nonce": authority_nonce,
                "external_call_id": logical.external_call_id,
                "side_effect_id": side_effect_id,
                "project_id": normalize_project_id(project_id),
                "data_residency": normalize_data_residency(data_residency),
                "run_id": str(logical_values.get("run_id") or ""),
                "task_id": str(logical_values.get("task_id") or ""),
                "max_attempts": int(max_attempts),
                "consumed_attempts": set(),
            }
        return ExternalAuthorityReservationV1(
            logical=logical,
            mode="transport_authorized",
            side_effect_id=side_effect_id,
            capability=capability,
        )

    def consume_for_transport(
        self,
        capability: ExternalTransportCapabilityV1,
        *,
        ordinal: int,
        endpoint: str,
        request_body: bytes,
    ) -> None:
        request_hash = hashlib.sha256(request_body).hexdigest()
        with self.security_store.active_project_operation(
            capability.project_id
        ):
            with self._lock:
                state = self._require_active_capability_locked(capability)
                if endpoint != state["endpoint"] or request_hash != state["request_hash"]:
                    raise ExternalCallAuthorityError(
                        "external transport capability does not match the admitted intent"
                    )
                logical = self.store.call(capability.external_call_id)
                side_effect = self.security_store.side_effect(
                    capability.side_effect_id
                )
                if logical is None or logical.get("status") != "reserved":
                    raise ExternalCallAuthorityError(
                        "external call is no longer transport-authorized"
                    )
                if side_effect is None or any(
                    item.get("status")
                    in {"completed", "failed", "ambiguous", "reconciled"}
                    for item in side_effect.get("events", ())
                ):
                    raise ExternalCallAuthorityError(
                        "external side effect is no longer transport-authorized"
                    )
                if not 1 <= int(ordinal) <= int(state["max_attempts"]):
                    raise ExternalCallAuthorityError(
                        "external attempt is outside capability"
                    )
                consumed = state["consumed_attempts"]
                if ordinal in consumed:
                    raise ExternalCallAuthorityError(
                        "external transport capability attempt was already consumed"
                    )
                consumed.add(ordinal)

    def guard_response(
        self,
        capability: ExternalTransportCapabilityV1,
        *,
        secret_lease: SecretLease | None,
        response_body: bytes,
    ) -> None:
        with self._lock:
            self._require_active_capability_locked(capability)
        if secret_lease is not None:
            secret_lease.reject_if_exposed(response_body)

    def record_side_effect(
        self,
        capability: ExternalTransportCapabilityV1,
        *,
        status: str,
        detail: Mapping[str, Any],
    ) -> None:
        if status not in {"completed", "failed", "ambiguous"}:
            raise ExternalCallAuthorityError(
                "capability cannot write this side-effect state"
            )
        with self.security_store.active_project_operation(
            capability.project_id
        ):
            with self._lock:
                state = self._require_active_capability_locked(capability)
                consumed = bool(state["consumed_attempts"])
                if status in {"completed", "ambiguous"} and not consumed:
                    raise ExternalCallAuthorityError(
                        "terminal side effect requires a consumed transport attempt"
                    )
                safe_detail = dict(detail)
                safe_detail["external_call_id"] = state["external_call_id"]
                safe_detail["transport_called"] = consumed
                self.security_store.record_side_effect_event(
                    capability.side_effect_id,
                    status=status,
                    detail=safe_detail,
                    authority_nonce=state["authority_nonce"],
                )
                self._capabilities.pop(capability.capability_id, None)

    def _require_active_capability_locked(
        self, capability: ExternalTransportCapabilityV1
    ) -> dict[str, Any]:
        state = self._capabilities.get(capability.capability_id)
        if state is None:
            raise ExternalCallAuthorityError(
                "external transport capability is missing or closed"
            )
        if (
            capability.external_call_id != state["external_call_id"]
            or capability.side_effect_id != state["side_effect_id"]
            or capability.project_id != state["project_id"]
            or capability.data_residency != state["data_residency"]
            or capability.endpoint != state["endpoint"]
            or capability.request_hash != state["request_hash"]
            or capability.provider_idempotency_key
            != state["provider_idempotency_key"]
            or capability.max_attempts != state["max_attempts"]
            or capability.schema_version != EXTERNAL_CALL_AUTHORITY_SCHEMA_VERSION
        ):
            raise ExternalCallAuthorityError(
                "external transport capability does not match the admitted intent"
            )
        return state

    def close_capability(self, capability: ExternalTransportCapabilityV1) -> None:
        with self._lock:
            self._capabilities.pop(capability.capability_id, None)

    def close_capabilities_for_request(
        self,
        *,
        project_id: str,
        data_residency: str,
        run_id: str,
        task_id: str,
    ) -> None:
        project = normalize_project_id(project_id)
        residency = normalize_data_residency(data_residency)
        with self._lock:
            stale = [
                capability_id
                for capability_id, state in self._capabilities.items()
                if state["project_id"] == project
                and state["data_residency"] == residency
                and state["run_id"] == str(run_id)
                and state["task_id"] == str(task_id)
            ]
            for capability_id in stale:
                self._capabilities.pop(capability_id, None)

    def active_capabilities(self) -> int:
        with self._lock:
            return len(self._capabilities)


__all__ = [
    "EXTERNAL_CALL_AUTHORITY_SCHEMA_VERSION",
    "ExternalAuthorityReservationV1",
    "ExternalCallAdmissionV1",
    "ExternalCallAuthorityError",
    "ExternalCallAuthorityV1",
    "ExternalTransportCapabilityV1",
]
