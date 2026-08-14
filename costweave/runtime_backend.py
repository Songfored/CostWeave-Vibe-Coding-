"""Provider-neutral Runtime Backend contract for v0.6.6 V066-WP06.

The runtime boundary is deliberately separate from model gateways.  A local
``RunManager`` is the first implementation; third-party runtimes may be
wrapped by an adapter implementing the same protocol, but no vendor-specific
runtime is imported or enabled by this module.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol, Sequence, runtime_checkable

from .commands import validate_idempotency_key
from .domain import RunMode, RunRequest, RunStatus
from .intelligence.common import (
    bounded_text,
    canonical_json,
    finite_number,
    stable_id,
    validate_exact_fields,
    validate_timestamp,
)
from .observability import sanitize_attributes
from .security_boundary import normalize_data_residency, normalize_project_id


RUNTIME_BACKEND_SCHEMA_VERSION = "v0.6.6"
RUNTIME_BACKEND_POLICY_REVISION = "runtime-backend-v1"
RUNTIME_OPERATION_TYPES = ("submit", "cancel", "checkpoint", "resume")
RUNTIME_STATUS_VALUES = tuple(item.value for item in RunStatus)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_TRACE_RE = re.compile(r"^[0-9a-f]{32}$")
_MAX_METADATA_BYTES = 32_000
_MAX_SNAPSHOT_BYTES = 2_000_000


class RuntimeBackendContractError(ValueError):
    """Raised when a runtime request or response violates the contract."""


class RuntimeBackendUnavailableError(RuntimeError):
    """Raised when the local persistent runtime is not enabled."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, name: str, *, minimum: int = 1, maximum: int = 256) -> str:
    try:
        return bounded_text(value, name, minimum=minimum, maximum=maximum)
    except (TypeError, ValueError) as exc:
        raise RuntimeBackendContractError(str(exc)) from exc


def _identifier(value: Any, name: str) -> str:
    text = _text(value, name)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise RuntimeBackendContractError(f"{name} contains unsupported identifier characters")
    return text


def _trace_id(value: Any) -> str:
    text = _text(value, "trace_id", maximum=32)
    if _TRACE_RE.fullmatch(text) is None:
        raise RuntimeBackendContractError("trace_id must be 32 lowercase hexadecimal characters")
    return text


def _integer(value: Any, name: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeBackendContractError(f"{name} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f" between {minimum} and {maximum}" if maximum is not None else f" >= {minimum}"
        raise RuntimeBackendContractError(f"{name} must be{bound}")
    return value


def _number(value: Any, name: str, *, minimum: float, maximum: float | None = None) -> float:
    try:
        number = finite_number(value, name, minimum=minimum)
    except (TypeError, ValueError) as exc:
        raise RuntimeBackendContractError(str(exc)) from exc
    if maximum is not None and number > maximum:
        raise RuntimeBackendContractError(f"{name} must be <= {maximum}")
    return number


def _optional_number(value: Any, name: str, *, minimum: float, maximum: float | None = None) -> float | None:
    if value is None:
        return None
    return _number(value, name, minimum=minimum, maximum=maximum)


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise RuntimeBackendContractError(f"{name} must be bool")
    return value


def _json_size(value: Any, name: str, maximum: int) -> None:
    try:
        raw = canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeBackendContractError(f"{name} must be finite JSON") from exc
    if len(raw.encode("utf-8")) > maximum:
        raise RuntimeBackendContractError(f"{name} exceeds {maximum} bytes")


def _contains_redaction(value: Any) -> bool:
    if isinstance(value, Mapping):
        if value.get("redacted") is True:
            return True
        return any(_contains_redaction(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_redaction(item) for item in value)
    return False


def _metadata(value: Mapping[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise RuntimeBackendContractError("metadata must be an object")
    try:
        safe = sanitize_attributes(value, maximum=_MAX_METADATA_BYTES)
    except Exception as exc:  # observability uses its own contract error
        raise RuntimeBackendContractError(str(exc)) from exc
    if _contains_redaction(safe):
        raise RuntimeBackendContractError("metadata must not contain secrets or prompt-like values")
    _json_size(safe, "metadata", _MAX_METADATA_BYTES)
    return copy.deepcopy(safe)


def _schema(value: Any) -> None:
    if value != RUNTIME_BACKEND_SCHEMA_VERSION:
        raise RuntimeBackendContractError(
            f"schema_version must be {RUNTIME_BACKEND_SCHEMA_VERSION}"
        )


def _timestamp(value: Any, name: str) -> str:
    try:
        return validate_timestamp(value, name)
    except (TypeError, ValueError) as exc:
        raise RuntimeBackendContractError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class RuntimeSubmitRequestV1:
    """Provider-neutral input for a local runtime submission."""

    goal: str
    idempotency_key: str
    project_id: str = "local-default"
    data_residency: str = "local"
    mode: str = "balanced"
    budget: float = 1.0
    quality_floor: float = 0.78
    max_concurrency: int = 4
    simulate_replan: bool = False
    deadline_seconds: float | None = None
    recovery_budget_ratio: float = 0.2
    value_of_time_per_second: float | None = None
    max_recoveries: int = 2
    max_task_attempts: int = 2
    recovery_time_limit_seconds: float = 30.0
    metadata: Mapping[str, Any] | None = None
    schema_version: str = RUNTIME_BACKEND_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "goal", _text(self.goal, "goal", minimum=6, maximum=20_000))
        try:
            object.__setattr__(self, "idempotency_key", validate_idempotency_key(self.idempotency_key))
        except (TypeError, ValueError) as exc:
            raise RuntimeBackendContractError(str(exc)) from exc
        try:
            object.__setattr__(self, "project_id", normalize_project_id(self.project_id))
            object.__setattr__(self, "data_residency", normalize_data_residency(self.data_residency))
        except (TypeError, ValueError) as exc:
            raise RuntimeBackendContractError(str(exc)) from exc
        try:
            object.__setattr__(self, "mode", RunMode(self.mode).value)
        except (TypeError, ValueError) as exc:
            raise RuntimeBackendContractError("mode must be economy, balanced, turbo or quality") from exc
        object.__setattr__(self, "budget", _number(self.budget, "budget", minimum=0.05, maximum=100.0))
        object.__setattr__(self, "quality_floor", _number(self.quality_floor, "quality_floor", minimum=0.5, maximum=0.99))
        object.__setattr__(self, "max_concurrency", _integer(self.max_concurrency, "max_concurrency", minimum=1, maximum=12))
        object.__setattr__(self, "simulate_replan", _bool(self.simulate_replan, "simulate_replan"))
        object.__setattr__(self, "deadline_seconds", _optional_number(self.deadline_seconds, "deadline_seconds", minimum=0.05, maximum=86_400.0))
        object.__setattr__(self, "recovery_budget_ratio", _number(self.recovery_budget_ratio, "recovery_budget_ratio", minimum=0.0, maximum=0.5))
        object.__setattr__(self, "value_of_time_per_second", _optional_number(self.value_of_time_per_second, "value_of_time_per_second", minimum=0.0, maximum=10.0))
        object.__setattr__(self, "max_recoveries", _integer(self.max_recoveries, "max_recoveries", minimum=0, maximum=8))
        object.__setattr__(self, "max_task_attempts", _integer(self.max_task_attempts, "max_task_attempts", minimum=1, maximum=5))
        object.__setattr__(self, "recovery_time_limit_seconds", _number(self.recovery_time_limit_seconds, "recovery_time_limit_seconds", minimum=0.1, maximum=86_400.0))
        object.__setattr__(self, "metadata", _metadata(self.metadata))
        _schema(self.schema_version)

    def to_run_request(self) -> RunRequest:
        return RunRequest(
            goal=self.goal,
            mode=RunMode(self.mode),
            budget=self.budget,
            quality_floor=self.quality_floor,
            max_concurrency=self.max_concurrency,
            simulate_replan=self.simulate_replan,
            deadline_seconds=self.deadline_seconds,
            recovery_budget_ratio=self.recovery_budget_ratio,
            value_of_time_per_second=self.value_of_time_per_second,
            max_recoveries=self.max_recoveries,
            max_task_attempts=self.max_task_attempts,
            recovery_time_limit_seconds=self.recovery_time_limit_seconds,
            project_id=self.project_id,
            data_residency=self.data_residency,
        ).normalized()

    def to_dict(self) -> dict[str, Any]:
        return {
            "goal": self.goal,
            "idempotency_key": self.idempotency_key,
            "project_id": self.project_id,
            "data_residency": self.data_residency,
            "mode": self.mode,
            "budget": self.budget,
            "quality_floor": self.quality_floor,
            "max_concurrency": self.max_concurrency,
            "simulate_replan": self.simulate_replan,
            "deadline_seconds": self.deadline_seconds,
            "recovery_budget_ratio": self.recovery_budget_ratio,
            "value_of_time_per_second": self.value_of_time_per_second,
            "max_recoveries": self.max_recoveries,
            "max_task_attempts": self.max_task_attempts,
            "recovery_time_limit_seconds": self.recovery_time_limit_seconds,
            "metadata": copy.deepcopy(dict(self.metadata or {})),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeSubmitRequestV1":
        required = frozenset({"goal", "idempotency_key"})
        optional = frozenset({
            "project_id", "data_residency", "mode", "budget", "quality_floor",
            "max_concurrency", "simulate_replan", "deadline_seconds",
            "recovery_budget_ratio", "value_of_time_per_second", "max_recoveries",
            "max_task_attempts", "recovery_time_limit_seconds", "metadata", "schema_version",
        })
        try:
            validate_exact_fields(value, required=required, optional=optional)
        except (TypeError, ValueError) as exc:
            raise RuntimeBackendContractError(str(exc)) from exc
        defaults = cls.__dataclass_fields__
        data = {name: value[name] if name in value else field.default for name, field in defaults.items() if name != "schema_version"}
        if "schema_version" in value:
            data["schema_version"] = value["schema_version"]
        return cls(**data)


@dataclass(frozen=True, slots=True)
class RuntimeCapabilitiesV1:
    backend_id: str = "local-runtime"
    backend_type: str = "local"
    persistent: bool = True
    submit: bool = True
    cancel: bool = True
    checkpoint: bool = True
    resume: bool = True
    events: bool = True
    external_calls_enabled: bool = False
    schema_version: str = RUNTIME_BACKEND_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend_id", _identifier(self.backend_id, "backend_id"))
        object.__setattr__(self, "backend_type", _identifier(self.backend_type, "backend_type"))
        for name in ("persistent", "submit", "cancel", "checkpoint", "resume", "events", "external_calls_enabled"):
            object.__setattr__(self, name, _bool(getattr(self, name), name))
        _schema(self.schema_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "backend_type": self.backend_type,
            "persistent": self.persistent,
            "submit": self.submit,
            "cancel": self.cancel,
            "checkpoint": self.checkpoint,
            "resume": self.resume,
            "events": self.events,
            "external_calls_enabled": self.external_calls_enabled,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class RuntimeOperationV1:
    operation: str
    run_id: str
    trace_id: str
    status: str
    runtime_revision: int
    replayed: bool
    snapshot: Mapping[str, Any]
    checkpoint_id: str | None = None
    occurred_at: str = ""
    schema_version: str = RUNTIME_BACKEND_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.operation not in RUNTIME_OPERATION_TYPES:
            raise RuntimeBackendContractError("unsupported runtime operation")
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        object.__setattr__(self, "trace_id", _trace_id(self.trace_id))
        if self.status not in RUNTIME_STATUS_VALUES:
            raise RuntimeBackendContractError("unsupported runtime status")
        object.__setattr__(self, "runtime_revision", _integer(self.runtime_revision, "runtime_revision"))
        object.__setattr__(self, "replayed", _bool(self.replayed, "replayed"))
        if not isinstance(self.snapshot, Mapping):
            raise RuntimeBackendContractError("snapshot must be an object")
        _json_size(self.snapshot, "snapshot", _MAX_SNAPSHOT_BYTES)
        object.__setattr__(self, "snapshot", copy.deepcopy(dict(self.snapshot)))
        if self.checkpoint_id is not None:
            object.__setattr__(self, "checkpoint_id", _identifier(self.checkpoint_id, "checkpoint_id"))
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at or _now(), "occurred_at"))
        _schema(self.schema_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "status": self.status,
            "runtime_revision": self.runtime_revision,
            "replayed": self.replayed,
            "snapshot": copy.deepcopy(dict(self.snapshot)),
            "checkpoint_id": self.checkpoint_id,
            "occurred_at": self.occurred_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "RuntimeOperationV1":
        required = frozenset({
            "operation", "run_id", "trace_id", "status", "runtime_revision", "replayed",
            "snapshot", "checkpoint_id", "occurred_at", "schema_version",
        })
        try:
            validate_exact_fields(value, required=required)
        except (TypeError, ValueError) as exc:
            raise RuntimeBackendContractError(str(exc)) from exc
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class RuntimeEventV1:
    run_id: str
    revision: int
    event_id: str
    kind: str
    occurred_at: str
    source: str
    task_id: str | None
    payload: Mapping[str, Any]
    command_id: str | None = None
    attempt_id: str | None = None
    schema_version: str = RUNTIME_BACKEND_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        object.__setattr__(self, "revision", _integer(self.revision, "revision", minimum=1))
        object.__setattr__(self, "event_id", _identifier(self.event_id, "event_id"))
        object.__setattr__(self, "kind", _identifier(self.kind, "kind"))
        object.__setattr__(self, "source", _text(self.source, "source", maximum=256))
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at, "occurred_at"))
        if self.task_id is not None:
            object.__setattr__(self, "task_id", _identifier(self.task_id, "task_id"))
        if self.command_id is not None:
            object.__setattr__(self, "command_id", _identifier(self.command_id, "command_id"))
        if self.attempt_id is not None:
            object.__setattr__(self, "attempt_id", _identifier(self.attempt_id, "attempt_id"))
        if not isinstance(self.payload, Mapping):
            raise RuntimeBackendContractError("event payload must be an object")
        try:
            safe_payload = sanitize_attributes(self.payload)
        except Exception as exc:
            raise RuntimeBackendContractError(str(exc)) from exc
        _json_size(safe_payload, "payload", _MAX_SNAPSHOT_BYTES)
        object.__setattr__(self, "payload", copy.deepcopy(safe_payload))
        _schema(self.schema_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "revision": self.revision,
            "event_id": self.event_id,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "source": self.source,
            "task_id": self.task_id,
            "payload": copy.deepcopy(dict(self.payload)),
            "command_id": self.command_id,
            "attempt_id": self.attempt_id,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class RuntimeEventPageV1:
    run_id: str
    after_revision: int
    next_revision: int
    has_more: bool
    events: tuple[RuntimeEventV1, ...]
    schema_version: str = RUNTIME_BACKEND_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _identifier(self.run_id, "run_id"))
        object.__setattr__(self, "after_revision", _integer(self.after_revision, "after_revision"))
        object.__setattr__(self, "next_revision", _integer(self.next_revision, "next_revision"))
        object.__setattr__(self, "has_more", _bool(self.has_more, "has_more"))
        events = tuple(self.events)
        if any(not isinstance(item, RuntimeEventV1) for item in events):
            raise RuntimeBackendContractError("events must contain RuntimeEventV1 items")
        revisions = [item.revision for item in events]
        if revisions != sorted(set(revisions)):
            raise RuntimeBackendContractError("event revisions must be strictly increasing")
        if any(item.run_id != self.run_id for item in events):
            raise RuntimeBackendContractError("event run_id does not match page")
        if events and self.next_revision != events[-1].revision:
            raise RuntimeBackendContractError("next_revision must match the last event")
        if not events and self.next_revision != self.after_revision:
            raise RuntimeBackendContractError("empty event page must retain after_revision")
        object.__setattr__(self, "events", events)
        _schema(self.schema_version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "after_revision": self.after_revision,
            "next_revision": self.next_revision,
            "has_more": self.has_more,
            "events": [item.to_dict() for item in self.events],
            "schema_version": self.schema_version,
        }


@runtime_checkable
class RuntimeBackend(Protocol):
    @property
    def capabilities(self) -> RuntimeCapabilitiesV1:
        ...

    def submit(self, request: RuntimeSubmitRequestV1) -> RuntimeOperationV1:
        ...

    def cancel(self, run_id: str, *, project_id: str, idempotency_key: str, expected_revision: int) -> RuntimeOperationV1:
        ...

    def checkpoint(self, run_id: str, *, project_id: str, idempotency_key: str, expected_revision: int, source: str = "client") -> RuntimeOperationV1:
        ...

    def resume(self, run_id: str, *, project_id: str, idempotency_key: str, expected_revision: int, source: str = "client") -> RuntimeOperationV1:
        ...

    def events(self, run_id: str, *, project_id: str, after_revision: int = 0, limit: int = 200) -> RuntimeEventPageV1:
        ...


class LocalRuntimeBackend:
    """Adapter exposing the existing persistent RunManager as a Runtime."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        persistent = getattr(manager, "control_kernel", None) is not None
        self._capabilities = RuntimeCapabilitiesV1(
            persistent=persistent,
            submit=persistent,
            cancel=persistent,
            checkpoint=persistent,
            resume=persistent,
            events=persistent,
        )

    @property
    def capabilities(self) -> RuntimeCapabilitiesV1:
        return self._capabilities

    def _require_persistent(self) -> None:
        if getattr(self.manager, "control_kernel", None) is None:
            raise RuntimeBackendUnavailableError("local runtime requires persistent runtime v4")

    def _record(self, run_id: str, project_id: str) -> Any:
        self._require_persistent()
        try:
            project = normalize_project_id(project_id)
        except (TypeError, ValueError) as exc:
            raise RuntimeBackendContractError(str(exc)) from exc
        record = self.manager.get(_identifier(run_id, "run_id"), project_id=project)
        if record is None:
            raise KeyError(run_id)
        return record

    @staticmethod
    def _operation(operation: str, record: Any, *, replayed: bool, checkpoint_id: str | None = None) -> RuntimeOperationV1:
        snapshot = record.snapshot()
        return RuntimeOperationV1(
            operation=operation,
            run_id=str(record.id),
            trace_id=str(record.trace_id),
            status=str(record.status.value),
            runtime_revision=int(record.runtime_revision),
            replayed=replayed,
            snapshot=snapshot,
            checkpoint_id=checkpoint_id,
        )

    def submit(self, request: RuntimeSubmitRequestV1) -> RuntimeOperationV1:
        if not isinstance(request, RuntimeSubmitRequestV1):
            if isinstance(request, Mapping):
                request = RuntimeSubmitRequestV1.from_dict(request)
            else:
                raise RuntimeBackendContractError("submit requires RuntimeSubmitRequestV1")
        self._require_persistent()
        record, replayed = self.manager.create_with_result(
            request.to_run_request(),
            idempotency_key=request.idempotency_key,
            command_metadata=dict(request.metadata or {}),
        )
        return self._operation("submit", record, replayed=replayed)

    def cancel(self, run_id: str, *, project_id: str, idempotency_key: str, expected_revision: int) -> RuntimeOperationV1:
        record = self._record(run_id, project_id)
        try:
            validate_idempotency_key(idempotency_key)
        except (TypeError, ValueError) as exc:
            raise RuntimeBackendContractError(str(exc)) from exc
        expected = _integer(expected_revision, "expected_revision")
        record, replayed = self.manager.cancel_with_result(
            str(record.id), idempotency_key=idempotency_key, expected_revision=expected
        )
        return self._operation("cancel", record, replayed=replayed)

    def _checkpoint_id(
        self,
        run_id: str,
        idempotency_key: str,
        source: str,
        operation: str,
    ) -> str:
        # Checkpoint and resume share the durable CHECKPOINT command kind, so
        # the operation must be part of the derived identity.  Otherwise a
        # caller could reuse one key across the two operations and receive a
        # false replay of the other action.
        return stable_id(
            "runtime_checkpoint",
            {
                "run_id": run_id,
                "idempotency_key": idempotency_key,
                "source": source,
                "operation": operation,
            },
        )

    def checkpoint(self, run_id: str, *, project_id: str, idempotency_key: str, expected_revision: int, source: str = "client") -> RuntimeOperationV1:
        record = self._record(run_id, project_id)
        try:
            validate_idempotency_key(idempotency_key)
        except (TypeError, ValueError) as exc:
            raise RuntimeBackendContractError(str(exc)) from exc
        source = _text(source, "source", maximum=128)
        checkpoint_id = self._checkpoint_id(
            str(record.id), idempotency_key, source, "checkpoint"
        )
        record, replayed = self.manager.checkpoint_with_result(
            str(record.id), idempotency_key=idempotency_key,
            expected_revision=_integer(expected_revision, "expected_revision"),
            checkpoint_id=checkpoint_id, source=source,
        )
        return self._operation("checkpoint", record, replayed=replayed, checkpoint_id=checkpoint_id)

    def resume(self, run_id: str, *, project_id: str, idempotency_key: str, expected_revision: int, source: str = "client") -> RuntimeOperationV1:
        record = self._record(run_id, project_id)
        try:
            validate_idempotency_key(idempotency_key)
        except (TypeError, ValueError) as exc:
            raise RuntimeBackendContractError(str(exc)) from exc
        source = _text(source, "source", maximum=128)
        checkpoint_id = self._checkpoint_id(
            str(record.id), idempotency_key, source, "resume"
        )
        record, replayed = self.manager.resume_with_result(
            str(record.id), idempotency_key=idempotency_key,
            expected_revision=_integer(expected_revision, "expected_revision"),
            checkpoint_id=checkpoint_id, source=source,
        )
        return self._operation("resume", record, replayed=replayed, checkpoint_id=checkpoint_id)

    def events(self, run_id: str, *, project_id: str, after_revision: int = 0, limit: int = 200) -> RuntimeEventPageV1:
        record = self._record(run_id, project_id)
        after = _integer(after_revision, "after_revision")
        page_limit = _integer(limit, "limit", minimum=1, maximum=1000)
        raw_events = self.manager.events(str(record.id), after_revision=after, limit=min(1000, page_limit + 1))
        has_more = len(raw_events) > page_limit
        raw_events = raw_events[:page_limit]
        events = tuple(
            RuntimeEventV1(
                run_id=str(item.get("run_id") or record.id),
                revision=int(item.get("revision", 0)),
                event_id=str(item.get("event_id") or stable_id("runtime_event", {"run_id": str(record.id), "revision": item.get("revision")})),
                kind=str(item.get("kind") or "runtime.event"),
                occurred_at=str(item.get("occurred_at") or _now()),
                source=str(item.get("source") or "runtime"),
                task_id=item.get("task_id"),
                payload=item.get("payload") or {},
                command_id=item.get("command_id"),
                attempt_id=item.get("attempt_id"),
            )
            for item in raw_events
        )
        return RuntimeEventPageV1(
            run_id=str(record.id),
            after_revision=after,
            next_revision=events[-1].revision if events else after,
            has_more=has_more,
            events=events,
        )


def public_runtime_backend_policy() -> dict[str, Any]:
    return {
        "schema_version": RUNTIME_BACKEND_SCHEMA_VERSION,
        "policy_revision": RUNTIME_BACKEND_POLICY_REVISION,
        "local_first": True,
        "persistent_runtime_required": True,
        "external_calls_enabled_by_default": False,
        "third_party_runtime_mode": "adapter-only",
        "supported_operations": list(RUNTIME_OPERATION_TYPES),
        "events_are_project_scoped": True,
        "checkpoint_is_idempotent": True,
        "resume_is_idempotent": True,
    }


__all__ = [
    "LocalRuntimeBackend",
    "RuntimeBackend",
    "RuntimeBackendContractError",
    "RuntimeBackendUnavailableError",
    "RuntimeCapabilitiesV1",
    "RuntimeEventPageV1",
    "RuntimeEventV1",
    "RuntimeOperationV1",
    "RuntimeSubmitRequestV1",
    "RUNTIME_BACKEND_POLICY_REVISION",
    "RUNTIME_BACKEND_SCHEMA_VERSION",
    "RUNTIME_OPERATION_TYPES",
    "public_runtime_backend_policy",
]
