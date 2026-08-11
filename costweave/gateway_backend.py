"""Provider-neutral Gateway Backend contracts for CostWeave v0.6.6.

This work package defines the control-plane boundary only.  It deliberately
does not open sockets, import a Provider SDK, persist prompts, or select a
vendor-specific request field.  Concrete OpenAI-compatible, LiteLLM and local
endpoint adapters are later work packages and must implement this contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol, Sequence, runtime_checkable

from .intelligence.common import (
    bounded_text,
    canonical_json,
    finite_number,
    stable_id,
    unique_texts,
    validate_exact_fields,
    validate_timestamp,
)
from .security_boundary import normalize_project_id


GATEWAY_SCHEMA_VERSION = "v0.6.6"
GATEWAY_POLICY_REVISION = "gateway-backend-contract-v1"
GATEWAY_PROTOCOLS = (
    "openai-compatible",
    "litellm",
    "local-openai-compatible",
    "custom",
)
GATEWAY_DEPLOYMENTS = ("mock", "local", "remote", "external_gateway")
GATEWAY_MODALITIES = ("text", "image", "audio", "video")
GATEWAY_ROLES = ("system", "user", "assistant", "tool")
GATEWAY_EVENT_TYPES = (
    "started",
    "delta",
    "tool_call",
    "usage",
    "completed",
    "error",
    "cancelled",
)
GATEWAY_STATUSES = ("completed", "failed", "cancelled")
GATEWAY_ERROR_CATEGORIES = (
    "disabled",
    "invalid_request",
    "authentication",
    "authorization",
    "rate_limit",
    "timeout",
    "overloaded",
    "cancelled",
    "network",
    "provider",
    "protocol",
    "unknown",
)
GATEWAY_BILLING_SOURCES = ("provider", "estimated", "missing")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer|password|private[_-]?key|secret)"
)
_SECRET_VALUE_RE = re.compile(r"(?i)(?:bearer\s+|sk-[A-Za-z0-9._~+/-]{8,})")
_MAX_CONTENT_BYTES = 2_000_000
_MAX_METADATA_BYTES = 32_000


class GatewayBackendContractError(ValueError):
    """Raised when a backend or gateway object violates the shared contract."""


class GatewayBackendDisabledError(GatewayBackendContractError):
    """Raised by a runtime that has not been explicitly enabled for calls."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _text(value: Any, name: str, *, minimum: int = 1, maximum: int = 192) -> str:
    try:
        return bounded_text(value, name, minimum=minimum, maximum=maximum)
    except (TypeError, ValueError) as exc:
        raise GatewayBackendContractError(str(exc)) from exc


def _identifier(value: Any, name: str) -> str:
    text = _text(value, name)
    if _IDENTIFIER_RE.fullmatch(text) is None:
        raise GatewayBackendContractError(f"{name} contains unsupported identifier characters")
    return text


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise GatewayBackendContractError(f"{name} must be a non-negative int")
    if value < 0:
        raise GatewayBackendContractError(f"{name} must be a non-negative int")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _non_negative_int(value, name)
    if result < 1:
        raise GatewayBackendContractError(f"{name} must be >= 1")
    return result


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise GatewayBackendContractError(f"{name} must be bool")
    return value


def _json_bytes(value: Any, name: str, *, maximum: int) -> str:
    try:
        raw = canonical_json(value)
    except (TypeError, ValueError) as exc:
        raise GatewayBackendContractError(f"{name} must be finite JSON") from exc
    if len(raw.encode("utf-8")) > maximum:
        raise GatewayBackendContractError(f"{name} exceeds {maximum} bytes")
    return raw


def _safe_metadata(value: Mapping[str, Any], name: str = "metadata") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise GatewayBackendContractError(f"{name} must be an object")
    result = dict(value)
    for key, item in result.items():
        if not isinstance(key, str) or not key.strip():
            raise GatewayBackendContractError(f"{name} keys must be non-empty strings")
        if _SECRET_KEY_RE.search(key) or (
            isinstance(item, str) and _SECRET_VALUE_RE.search(item)
        ):
            raise GatewayBackendContractError(f"{name} must not contain secrets")
    _json_bytes(result, name, maximum=_MAX_METADATA_BYTES)
    return result


def _optional_identifier(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _identifier(value, name)


def _timestamp(value: Any, name: str) -> str:
    try:
        return validate_timestamp(value, name)
    except (TypeError, ValueError) as exc:
        raise GatewayBackendContractError(str(exc)) from exc


@dataclass(frozen=True, slots=True)
class GatewayCapabilitiesV1:
    """Capabilities advertised by a backend without exposing provider fields."""

    backend_id: str
    protocol: str
    deployment: str
    modalities: tuple[str, ...] = ("text",)
    supports_streaming: bool = False
    supports_cancellation: bool = False
    supports_structured_output: bool = False
    supports_tools: bool = False
    reports_usage: bool = False
    reports_billing: bool = False
    reports_queue_time: bool = False
    max_context_tokens: int = 0
    max_output_tokens: int = 0
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "backend_id", _identifier(self.backend_id, "backend_id"))
        if self.protocol not in GATEWAY_PROTOCOLS:
            raise GatewayBackendContractError("unsupported gateway protocol")
        if self.deployment not in GATEWAY_DEPLOYMENTS:
            raise GatewayBackendContractError("unsupported gateway deployment")
        modalities = unique_texts(self.modalities, "modalities", maximum_items=8)
        if not modalities or any(item not in GATEWAY_MODALITIES for item in modalities):
            raise GatewayBackendContractError("unsupported gateway modality")
        object.__setattr__(self, "modalities", modalities)
        for name in (
            "supports_streaming",
            "supports_cancellation",
            "supports_structured_output",
            "supports_tools",
            "reports_usage",
            "reports_billing",
            "reports_queue_time",
        ):
            object.__setattr__(self, name, _bool(getattr(self, name), name))
        object.__setattr__(self, "max_context_tokens", _non_negative_int(self.max_context_tokens, "max_context_tokens"))
        object.__setattr__(self, "max_output_tokens", _non_negative_int(self.max_output_tokens, "max_output_tokens"))
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise GatewayBackendContractError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "protocol": self.protocol,
            "deployment": self.deployment,
            "modalities": list(self.modalities),
            "supports_streaming": self.supports_streaming,
            "supports_cancellation": self.supports_cancellation,
            "supports_structured_output": self.supports_structured_output,
            "supports_tools": self.supports_tools,
            "reports_usage": self.reports_usage,
            "reports_billing": self.reports_billing,
            "reports_queue_time": self.reports_queue_time,
            "max_context_tokens": self.max_context_tokens,
            "max_output_tokens": self.max_output_tokens,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayCapabilitiesV1":
        required = frozenset({
            "backend_id", "protocol", "deployment", "modalities",
            "supports_streaming", "supports_cancellation",
            "supports_structured_output", "supports_tools", "reports_usage",
            "reports_billing", "reports_queue_time", "max_context_tokens",
            "max_output_tokens", "schema_version",
        })
        validate_exact_fields(value, required=required)
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class GatewayMessageV1:
    role: str
    content: Any
    name: str | None = None
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.role not in GATEWAY_ROLES:
            raise GatewayBackendContractError("unsupported gateway message role")
        _json_bytes(self.content, "content", maximum=_MAX_CONTENT_BYTES)
        if self.name is not None:
            object.__setattr__(self, "name", _identifier(self.name, "name"))
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise GatewayBackendContractError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name is not None:
            result["name"] = self.name
        result["schema_version"] = self.schema_version
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayMessageV1":
        validate_exact_fields(
            value,
            required=frozenset({"role", "content", "schema_version"}),
            optional=frozenset({"name"}),
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class GatewayToolV1:
    name: str
    input_schema: Mapping[str, Any]
    description: str = ""
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _identifier(self.name, "tool name"))
        object.__setattr__(self, "description", _text(self.description, "description", minimum=0, maximum=1000))
        if not isinstance(self.input_schema, Mapping):
            raise GatewayBackendContractError("tool input_schema must be an object")
        _json_bytes(self.input_schema, "input_schema", maximum=100_000)
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise GatewayBackendContractError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": dict(self.input_schema),
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayToolV1":
        validate_exact_fields(
            value,
            required=frozenset({"name", "description", "input_schema", "schema_version"}),
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class GatewayRequestV1:
    """Provider-neutral request passed from the decision plane to a backend."""

    request_id: str
    trace_id: str
    project_id: str
    model: str
    messages: tuple[GatewayMessageV1, ...]
    max_output_tokens: int
    idempotency_key: str
    tools: tuple[GatewayToolV1, ...] = ()
    response_format: Mapping[str, Any] | None = None
    temperature: float | None = None
    stream: bool = False
    metadata: Mapping[str, Any] | None = None
    created_at: str = ""
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        object.__setattr__(self, "trace_id", _identifier(self.trace_id, "trace_id"))
        try:
            project_id = normalize_project_id(self.project_id)
        except (TypeError, ValueError) as exc:
            raise GatewayBackendContractError(str(exc)) from exc
        object.__setattr__(self, "project_id", project_id)
        object.__setattr__(self, "model", _identifier(self.model, "model"))
        messages = tuple(self.messages)
        if not 1 <= len(messages) <= 4096 or any(not isinstance(item, GatewayMessageV1) for item in messages):
            raise GatewayBackendContractError("messages must contain 1-4096 GatewayMessageV1 items")
        object.__setattr__(self, "messages", messages)
        tools = tuple(self.tools)
        if len(tools) > 128 or any(not isinstance(item, GatewayToolV1) for item in tools):
            raise GatewayBackendContractError("tools must contain at most 128 GatewayToolV1 items")
        if len({item.name for item in tools}) != len(tools):
            raise GatewayBackendContractError("tool names must be unique")
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "max_output_tokens", _positive_int(self.max_output_tokens, "max_output_tokens"))
        object.__setattr__(self, "idempotency_key", _identifier(self.idempotency_key, "idempotency_key"))
        if self.response_format is not None:
            if not isinstance(self.response_format, Mapping):
                raise GatewayBackendContractError("response_format must be an object")
            _json_bytes(self.response_format, "response_format", maximum=32_000)
            object.__setattr__(self, "response_format", dict(self.response_format))
        if self.temperature is not None:
            temperature = finite_number(self.temperature, "temperature", minimum=0.0)
            if temperature > 2.0:
                raise GatewayBackendContractError("temperature must be <= 2")
            object.__setattr__(self, "temperature", temperature)
        object.__setattr__(self, "stream", _bool(self.stream, "stream"))
        metadata = {} if self.metadata is None else _safe_metadata(self.metadata)
        object.__setattr__(self, "metadata", metadata)
        object.__setattr__(self, "created_at", _timestamp(self.created_at or _now(), "created_at"))
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise GatewayBackendContractError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    @classmethod
    def create(
        cls,
        *,
        trace_id: str,
        project_id: str,
        model: str,
        messages: Sequence[GatewayMessageV1],
        max_output_tokens: int,
        idempotency_key: str,
        **kwargs: Any,
    ) -> "GatewayRequestV1":
        if any(not isinstance(item, GatewayMessageV1) for item in messages):
            raise GatewayBackendContractError("messages must contain GatewayMessageV1 items")
        tools = tuple(kwargs.get("tools", ()))
        if any(not isinstance(item, GatewayToolV1) for item in tools):
            raise GatewayBackendContractError("tools must contain GatewayToolV1 items")
        metadata = kwargs.get("metadata")
        identity = {
            "trace_id": trace_id,
            "project_id": project_id,
            "model": model,
            "messages": [item.to_dict() for item in messages],
            "max_output_tokens": max_output_tokens,
            "idempotency_key": idempotency_key,
            "tools": [item.to_dict() for item in tools],
            "response_format": kwargs.get("response_format"),
            "temperature": kwargs.get("temperature"),
            "stream": kwargs.get("stream", False),
            "metadata": {} if metadata is None else metadata,
        }
        return cls(
            request_id=stable_id("gateway_request", identity),
            trace_id=trace_id,
            project_id=project_id,
            model=model,
            messages=tuple(messages),
            max_output_tokens=max_output_tokens,
            idempotency_key=idempotency_key,
            **kwargs,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "trace_id": self.trace_id,
            "project_id": self.project_id,
            "model": self.model,
            "messages": [item.to_dict() for item in self.messages],
            "max_output_tokens": self.max_output_tokens,
            "idempotency_key": self.idempotency_key,
            "tools": [item.to_dict() for item in self.tools],
            "response_format": None if self.response_format is None else dict(self.response_format),
            "temperature": self.temperature,
            "stream": self.stream,
            "metadata": dict(self.metadata),
            "created_at": self.created_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayRequestV1":
        required = frozenset({
            "request_id", "trace_id", "project_id", "model", "messages",
            "max_output_tokens", "idempotency_key", "tools", "response_format",
            "temperature", "stream", "metadata", "created_at", "schema_version",
        })
        validate_exact_fields(value, required=required)
        data = dict(value)
        data["messages"] = tuple(GatewayMessageV1.from_dict(item) for item in data["messages"])
        data["tools"] = tuple(GatewayToolV1.from_dict(item) for item in data["tools"])
        return cls(**data)


@dataclass(frozen=True, slots=True)
class GatewayUsageV1:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    reasoning_tokens: int = 0
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens"):
            object.__setattr__(self, name, _non_negative_int(getattr(self, name), name))
        if self.cached_input_tokens > self.input_tokens:
            raise GatewayBackendContractError("cached_input_tokens cannot exceed input_tokens")
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise GatewayBackendContractError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cached_input_tokens": self.cached_input_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayUsageV1":
        validate_exact_fields(
            value,
            required=frozenset({"input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "total_tokens", "schema_version"}),
        )
        item = cls(**{key: value[key] for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens", "schema_version")})
        if value["total_tokens"] != item.total_tokens:
            raise GatewayBackendContractError("total_tokens does not match usage fields")
        return item


@dataclass(frozen=True, slots=True)
class GatewayBillingV1:
    source: str = "missing"
    currency: str = "USD"
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0
    cached_input_cost_usd: float = 0.0
    unallocated_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
    reconciled: bool = False
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.source not in GATEWAY_BILLING_SOURCES:
            raise GatewayBackendContractError("unsupported billing source")
        if not isinstance(self.currency, str) or _CURRENCY_RE.fullmatch(self.currency) is None:
            raise GatewayBackendContractError("currency must be an uppercase ISO-like code")
        costs = {}
        for name in ("input_cost_usd", "output_cost_usd", "cached_input_cost_usd", "unallocated_cost_usd", "total_cost_usd"):
            costs[name] = finite_number(getattr(self, name), name, minimum=0.0)
            object.__setattr__(self, name, costs[name])
        subtotal = sum(costs[name] for name in ("input_cost_usd", "output_cost_usd", "cached_input_cost_usd", "unallocated_cost_usd"))
        if costs["total_cost_usd"] + 1e-12 < subtotal:
            raise GatewayBackendContractError("total_cost_usd is below itemized billing")
        if self.source == "missing" and any(costs[name] > 0 for name in costs):
            raise GatewayBackendContractError("missing billing cannot contain cost values")
        object.__setattr__(self, "reconciled", _bool(self.reconciled, "reconciled"))
        if self.source == "missing" and self.reconciled:
            raise GatewayBackendContractError("missing billing cannot be reconciled")
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise GatewayBackendContractError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "currency": self.currency,
            "input_cost_usd": self.input_cost_usd,
            "output_cost_usd": self.output_cost_usd,
            "cached_input_cost_usd": self.cached_input_cost_usd,
            "unallocated_cost_usd": self.unallocated_cost_usd,
            "total_cost_usd": self.total_cost_usd,
            "reconciled": self.reconciled,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayBillingV1":
        required = frozenset({
            "source", "currency", "input_cost_usd", "output_cost_usd",
            "cached_input_cost_usd", "unallocated_cost_usd", "total_cost_usd",
            "reconciled", "schema_version",
        })
        validate_exact_fields(value, required=required)
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class GatewayErrorV1:
    category: str
    message: str
    retryable: bool
    provider_code: str | None = None
    provider_request_id: str | None = None
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.category not in GATEWAY_ERROR_CATEGORIES:
            raise GatewayBackendContractError("unsupported gateway error category")
        message = _text(self.message, "message", maximum=300)
        if _SECRET_VALUE_RE.search(message):
            raise GatewayBackendContractError("gateway error message contains a secret")
        object.__setattr__(self, "message", message)
        object.__setattr__(self, "retryable", _bool(self.retryable, "retryable"))
        object.__setattr__(self, "provider_code", _optional_identifier(self.provider_code, "provider_code"))
        object.__setattr__(self, "provider_request_id", _optional_identifier(self.provider_request_id, "provider_request_id"))
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise GatewayBackendContractError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "message": self.message,
            "retryable": self.retryable,
            "provider_code": self.provider_code,
            "provider_request_id": self.provider_request_id,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayErrorV1":
        validate_exact_fields(
            value,
            required=frozenset({"category", "message", "retryable", "provider_code", "provider_request_id", "schema_version"}),
        )
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class GatewayStreamEventV1:
    request_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    provider_request_id: str | None = None
    event_id: str = ""
    occurred_at: str = ""
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        object.__setattr__(self, "sequence", _non_negative_int(self.sequence, "sequence"))
        if self.event_type not in GATEWAY_EVENT_TYPES:
            raise GatewayBackendContractError("unsupported gateway event type")
        if not isinstance(self.payload, Mapping):
            raise GatewayBackendContractError("event payload must be an object")
        _json_bytes(self.payload, "payload", maximum=_MAX_CONTENT_BYTES)
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "provider_request_id", _optional_identifier(self.provider_request_id, "provider_request_id"))
        object.__setattr__(self, "event_id", _identifier(self.event_id or stable_id("gateway_event", {"request_id": self.request_id, "sequence": self.sequence, "event_type": self.event_type, "payload": self.payload}), "event_id"))
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at or _now(), "occurred_at"))
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise GatewayBackendContractError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "sequence": self.sequence,
            "event_type": self.event_type,
            "payload": dict(self.payload),
            "provider_request_id": self.provider_request_id,
            "event_id": self.event_id,
            "occurred_at": self.occurred_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayStreamEventV1":
        required = frozenset({"request_id", "sequence", "event_type", "payload", "provider_request_id", "event_id", "occurred_at", "schema_version"})
        validate_exact_fields(value, required=required)
        return cls(**dict(value))


@dataclass(frozen=True, slots=True)
class GatewayResponseV1:
    request_id: str
    status: str
    content: str = ""
    structured_output: Mapping[str, Any] | Sequence[Any] | None = None
    tool_calls: tuple[Mapping[str, Any], ...] = ()
    usage: GatewayUsageV1 | None = None
    billing: GatewayBillingV1 | None = None
    provider_request_id: str | None = None
    error: GatewayErrorV1 | None = None
    events: tuple[GatewayStreamEventV1, ...] = ()
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        if self.status not in GATEWAY_STATUSES:
            raise GatewayBackendContractError("unsupported gateway response status")
        object.__setattr__(self, "content", _text(self.content, "content", minimum=0, maximum=_MAX_CONTENT_BYTES))
        if self.structured_output is not None:
            _json_bytes(self.structured_output, "structured_output", maximum=_MAX_CONTENT_BYTES)
        tools = tuple(self.tool_calls)
        if any(not isinstance(item, Mapping) for item in tools):
            raise GatewayBackendContractError("tool_calls must contain objects")
        for item in tools:
            _json_bytes(item, "tool_call", maximum=100_000)
        object.__setattr__(self, "tool_calls", tuple(dict(item) for item in tools))
        if self.usage is not None and not isinstance(self.usage, GatewayUsageV1):
            raise GatewayBackendContractError("usage must be GatewayUsageV1")
        if self.billing is not None and not isinstance(self.billing, GatewayBillingV1):
            raise GatewayBackendContractError("billing must be GatewayBillingV1")
        object.__setattr__(self, "provider_request_id", _optional_identifier(self.provider_request_id, "provider_request_id"))
        if self.error is not None and not isinstance(self.error, GatewayErrorV1):
            raise GatewayBackendContractError("error must be GatewayErrorV1")
        if self.status == "completed" and self.error is not None:
            raise GatewayBackendContractError("completed response cannot contain an error")
        if self.status in {"failed", "cancelled"} and self.error is None:
            raise GatewayBackendContractError("failed or cancelled response requires an error")
        events = tuple(self.events)
        if any(not isinstance(item, GatewayStreamEventV1) for item in events):
            raise GatewayBackendContractError("events must contain GatewayStreamEventV1 items")
        sequences = [item.sequence for item in events]
        if sequences != sorted(set(sequences)):
            raise GatewayBackendContractError("event sequences must be strictly increasing")
        if any(item.request_id != self.request_id for item in events):
            raise GatewayBackendContractError("event request_id does not match response")
        object.__setattr__(self, "events", events)
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise GatewayBackendContractError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "status": self.status,
            "content": self.content,
            "structured_output": self.structured_output,
            "tool_calls": [dict(item) for item in self.tool_calls],
            "usage": None if self.usage is None else self.usage.to_dict(),
            "billing": None if self.billing is None else self.billing.to_dict(),
            "provider_request_id": self.provider_request_id,
            "error": None if self.error is None else self.error.to_dict(),
            "events": [item.to_dict() for item in self.events],
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayResponseV1":
        required = frozenset({"request_id", "status", "content", "structured_output", "tool_calls", "usage", "billing", "provider_request_id", "error", "events", "schema_version"})
        validate_exact_fields(value, required=required)
        data = dict(value)
        data["usage"] = None if data["usage"] is None else GatewayUsageV1.from_dict(data["usage"])
        data["billing"] = None if data["billing"] is None else GatewayBillingV1.from_dict(data["billing"])
        data["error"] = None if data["error"] is None else GatewayErrorV1.from_dict(data["error"])
        data["events"] = tuple(GatewayStreamEventV1.from_dict(item) for item in data["events"])
        return cls(**data)


@dataclass(frozen=True, slots=True)
class GatewayCancellationV1:
    request_id: str
    accepted: bool
    reason: str
    provider_request_id: str | None = None
    occurred_at: str = ""
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _identifier(self.request_id, "request_id"))
        object.__setattr__(self, "accepted", _bool(self.accepted, "accepted"))
        object.__setattr__(self, "reason", _text(self.reason, "reason", maximum=300))
        object.__setattr__(self, "provider_request_id", _optional_identifier(self.provider_request_id, "provider_request_id"))
        object.__setattr__(self, "occurred_at", _timestamp(self.occurred_at or _now(), "occurred_at"))
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise GatewayBackendContractError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "accepted": self.accepted,
            "reason": self.reason,
            "provider_request_id": self.provider_request_id,
            "occurred_at": self.occurred_at,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "GatewayCancellationV1":
        validate_exact_fields(
            value,
            required=frozenset({
                "request_id", "accepted", "reason", "provider_request_id",
                "occurred_at", "schema_version",
            }),
        )
        return cls(**dict(value))


@runtime_checkable
class GatewayBackend(Protocol):
    """Minimal runtime interface implemented by later gateway adapters."""

    @property
    def capabilities(self) -> GatewayCapabilitiesV1:
        ...

    def submit(self, request: GatewayRequestV1) -> GatewayResponseV1:
        ...

    def stream(self, request: GatewayRequestV1) -> Iterable[GatewayStreamEventV1]:
        ...

    def cancel(self, request_id: str, *, reason: str) -> GatewayCancellationV1:
        ...


def validate_response_for_request(
    request: GatewayRequestV1, response: GatewayResponseV1
) -> GatewayResponseV1:
    """Enforce the cross-object boundary before a response reaches the planner."""

    if response.request_id != request.request_id:
        raise GatewayBackendContractError("response request_id does not match request")
    if request.stream and response.status == "completed" and not response.events:
        raise GatewayBackendContractError("streaming request requires at least one event")
    if not request.stream and response.events:
        raise GatewayBackendContractError("non-streaming request cannot contain stream events")
    return response


def public_gateway_policy() -> dict[str, Any]:
    """Expose safe defaults without exposing credentials or provider internals."""

    return {
        "schema_version": GATEWAY_SCHEMA_VERSION,
        "policy_revision": GATEWAY_POLICY_REVISION,
        "network_enabled_by_default": False,
        "provider_calls_enabled_by_default": False,
        "paid_calls_enabled_by_default": False,
        "runtime_authority": "contract-only",
        "supported_protocols": list(GATEWAY_PROTOCOLS),
        "required_response_facts": ["status", "usage", "billing", "provider_request_id"],
        "provider_specific_fields_in_core": False,
        "telemetry_prompt_storage": False,
    }


__all__ = [
    "GATEWAY_BILLING_SOURCES",
    "GATEWAY_DEPLOYMENTS",
    "GATEWAY_ERROR_CATEGORIES",
    "GATEWAY_EVENT_TYPES",
    "GATEWAY_MODALITIES",
    "GATEWAY_POLICY_REVISION",
    "GATEWAY_PROTOCOLS",
    "GATEWAY_ROLES",
    "GATEWAY_SCHEMA_VERSION",
    "GATEWAY_STATUSES",
    "GatewayBackend",
    "GatewayBackendContractError",
    "GatewayBackendDisabledError",
    "GatewayBillingV1",
    "GatewayCapabilitiesV1",
    "GatewayCancellationV1",
    "GatewayErrorV1",
    "GatewayMessageV1",
    "GatewayRequestV1",
    "GatewayResponseV1",
    "GatewayStreamEventV1",
    "GatewayToolV1",
    "GatewayUsageV1",
    "public_gateway_policy",
    "validate_response_for_request",
]
