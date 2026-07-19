"""Versioned, JSON-safe contracts introduced by CostWeave v0.4.1."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, NewType


CONTRACT_SCHEMA_VERSION = "v4.1"

RunId = NewType("RunId", str)
TaskId = NewType("TaskId", str)
EventId = NewType("EventId", str)
ArtifactId = NewType("ArtifactId", str)
CommandId = NewType("CommandId", str)

_ID_KINDS = frozenset({"run", "task", "event", "artifact", "command"})
_ID_PATTERN = re.compile(
    r"^(?P<kind>run|task|event|artifact|command)_(?P<value>[0-9a-f]{32})$"
)
_KIND_PATTERN = re.compile(r"^[a-z][a-z0-9_.-]{1,63}$")


class ContractError(ValueError):
    """Raised when a v4 contract is malformed."""


class SchemaVersionError(ContractError):
    """Raised when a payload uses an unsupported contract schema."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def new_identifier(kind: str) -> str:
    normalized = _validate_kind(kind)
    return f"{normalized}_{uuid.uuid4().hex}"


def validate_identifier(value: str, expected_kind: str | None = None) -> str:
    if not isinstance(value, str):
        raise ContractError("identifier must be a string")
    match = _ID_PATTERN.fullmatch(value)
    if match is None:
        raise ContractError(
            "identifier must use '<kind>_<32 lowercase hex characters>'"
        )
    if expected_kind is not None:
        normalized = _validate_kind(expected_kind)
        if match.group("kind") != normalized:
            raise ContractError(
                f"identifier kind must be {normalized}, got {match.group('kind')}"
            )
    return value


def canonical_identifier(kind: str, legacy_value: str) -> str:
    """Return a stable v4 ID while preserving legacy IDs outside the contract."""

    normalized = _validate_kind(kind)
    raw = str(legacy_value)
    try:
        return validate_identifier(raw, normalized)
    except ContractError:
        digest = hashlib.sha256(f"{normalized}:{raw}".encode("utf-8")).hexdigest()[:32]
        return f"{normalized}_{digest}"


def new_run_id() -> RunId:
    return RunId(new_identifier("run"))


def new_task_id() -> TaskId:
    return TaskId(new_identifier("task"))


def new_event_id() -> EventId:
    return EventId(new_identifier("event"))


def new_artifact_id() -> ArtifactId:
    return ArtifactId(new_identifier("artifact"))


def new_command_id() -> CommandId:
    return CommandId(new_identifier("command"))


def _validate_kind(kind: str) -> str:
    normalized = str(kind).strip().lower()
    if normalized not in _ID_KINDS:
        raise ContractError(f"unsupported identifier kind: {kind}")
    return normalized


def _validate_schema(value: Any) -> str:
    if value != CONTRACT_SCHEMA_VERSION:
        raise SchemaVersionError(
            f"schema_version must be {CONTRACT_SCHEMA_VERSION}, got {value!r}"
        )
    return str(value)


def _validate_message_kind(value: Any) -> str:
    if not isinstance(value, str) or _KIND_PATTERN.fullmatch(value) is None:
        raise ContractError(
            "kind must start with a lowercase letter and contain 2-64 "
            "lowercase letters, digits, dots, underscores or hyphens"
        )
    return value


def _validate_timestamp(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{field_name} must be a non-empty ISO-8601 string")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field_name} must be an ISO-8601 timestamp") from exc
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return copy.deepcopy(value)


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _validate_payload(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ContractError("payload must be a JSON object")
    thawed = _thaw(value)
    try:
        json.dumps(thawed, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ContractError("payload must contain only finite JSON values") from exc
    return _freeze(thawed)


def _read_json_object(raw: str) -> dict[str, Any]:
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ContractError("contract JSON is invalid") from exc
    if not isinstance(payload, dict):
        raise ContractError("contract JSON must contain an object")
    return payload


def _validate_fields(
    data: Mapping[str, Any],
    required: frozenset[str],
    optional: frozenset[str],
) -> None:
    missing = required - data.keys()
    unknown = data.keys() - required - optional
    if missing:
        raise ContractError(f"missing contract fields: {sorted(missing)}")
    if unknown:
        raise ContractError(f"unknown contract fields: {sorted(unknown)}")


@dataclass(frozen=True, slots=True)
class CommandEnvelope:
    command_id: str
    run_id: str
    kind: str
    issued_at: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_identifier(self.command_id, "command")
        validate_identifier(self.run_id, "run")
        if self.task_id is not None:
            validate_identifier(self.task_id, "task")
        _validate_message_kind(self.kind)
        _validate_timestamp(self.issued_at, "issued_at")
        _validate_schema(self.schema_version)
        object.__setattr__(self, "payload", _validate_payload(self.payload))

    @classmethod
    def create(
        cls,
        run_id: str,
        kind: str,
        payload: Mapping[str, Any] | None = None,
        *,
        task_id: str | None = None,
    ) -> "CommandEnvelope":
        return cls(
            command_id=str(new_command_id()),
            run_id=run_id,
            kind=kind,
            issued_at=utc_now(),
            payload=payload or {},
            task_id=task_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "command_id": self.command_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "issued_at": self.issued_at,
            "payload": _thaw(self.payload),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CommandEnvelope":
        if not isinstance(data, Mapping):
            raise ContractError("command envelope must be an object")
        _validate_fields(
            data,
            frozenset(
                {
                    "schema_version",
                    "command_id",
                    "run_id",
                    "kind",
                    "issued_at",
                    "payload",
                }
            ),
            frozenset({"task_id"}),
        )
        return cls(
            schema_version=data["schema_version"],
            command_id=data["command_id"],
            run_id=data["run_id"],
            task_id=data.get("task_id"),
            kind=data["kind"],
            issued_at=data["issued_at"],
            payload=data["payload"],
        )

    @classmethod
    def from_json(cls, raw: str) -> "CommandEnvelope":
        return cls.from_dict(_read_json_object(raw))


@dataclass(frozen=True, slots=True)
class EventEnvelope:
    event_id: str
    run_id: str
    kind: str
    occurred_at: str
    source: str
    payload: Mapping[str, Any] = field(default_factory=dict)
    task_id: str | None = None
    schema_version: str = CONTRACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        validate_identifier(self.event_id, "event")
        validate_identifier(self.run_id, "run")
        if self.task_id is not None:
            validate_identifier(self.task_id, "task")
        _validate_message_kind(self.kind)
        _validate_timestamp(self.occurred_at, "occurred_at")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ContractError("source must be a non-empty string")
        _validate_schema(self.schema_version)
        object.__setattr__(self, "payload", _validate_payload(self.payload))

    @classmethod
    def create(
        cls,
        run_id: str,
        kind: str,
        source: str,
        payload: Mapping[str, Any] | None = None,
        *,
        task_id: str | None = None,
    ) -> "EventEnvelope":
        return cls(
            event_id=str(new_event_id()),
            run_id=run_id,
            kind=kind,
            occurred_at=utc_now(),
            source=source,
            payload=payload or {},
            task_id=task_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "kind": self.kind,
            "occurred_at": self.occurred_at,
            "source": self.source,
            "payload": _thaw(self.payload),
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "EventEnvelope":
        if not isinstance(data, Mapping):
            raise ContractError("event envelope must be an object")
        _validate_fields(
            data,
            frozenset(
                {
                    "schema_version",
                    "event_id",
                    "run_id",
                    "kind",
                    "occurred_at",
                    "source",
                    "payload",
                }
            ),
            frozenset({"task_id"}),
        )
        return cls(
            schema_version=data["schema_version"],
            event_id=data["event_id"],
            run_id=data["run_id"],
            task_id=data.get("task_id"),
            kind=data["kind"],
            occurred_at=data["occurred_at"],
            source=data["source"],
            payload=data["payload"],
        )

    @classmethod
    def from_json(cls, raw: str) -> "EventEnvelope":
        return cls.from_dict(_read_json_object(raw))
