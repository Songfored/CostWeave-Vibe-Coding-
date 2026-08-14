"""Bounded OpenTelemetry-compatible tracing for the v0.6.6 runtime.

WP05 intentionally uses only the Python standard library.  It provides a
small trace/span contract that can be exported as OTLP-shaped JSON, while
keeping recording local and external export disabled by default.  Prompts,
credentials and provider payloads are never stored in spans.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import uuid
import weakref
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Iterator, Mapping


OBSERVABILITY_SCHEMA_VERSION = "v0.6.6-otel-v1"
OBSERVABILITY_POLICY_REVISION = "otel-compatible-local-trace-v1"
TRACE_STAGES = (
    "goal",
    "plan",
    "task",
    "provider_call",
    "validation",
    "recovery",
)
TRACE_KINDS = ("internal", "client", "server", "producer", "consumer")
TRACE_STATUSES = ("unset", "ok", "error")
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_RE = re.compile(r"^[0-9a-f]{16}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_.:-]{1,127}$")
_ATTRIBUTE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SECRET_KEY_RE = re.compile(
    r"(?i)(?:"
    r"api[_\-.:]*key|access[_\-.:]*key|account[_\-.:]*key|"
    r"client[_\-.:]*secret|authorization|authentication|"
    r"(?:^|[_\-.:])auth(?:$|[_\-.:])|"
    r"bearer|password|passwd|passphrase|"
    r"(?:^|[_\-.:])(?:pass|pwd|oauth|dsn)(?:$|[_\-.:])|"
    r"(?:database|db)[_\-.:]*(?:url|uri)|"
    r"connection[_\-.:]*(?:string|url|uri)|"
    r"private[_\-.:]*key|signing[_\-.:]*key|ssh[_\-.:]*key|"
    r"secret|credential|cookie|session(?:[_\-.:]*id)?|"
    r"(?:^|[_\-.:])jwt(?:$|[_\-.:])|token"
    r")"
)
_PROMPT_KEY_RE = re.compile(
    r"(?i)^(?:prompts?|contents?|messages?|conversations?|inputs?|outputs?|"
    r"body|text|goal|objective|query|completion|response|payload|data|context|"
    r"instructions?|description)$"
)
_SECRET_VALUE_RE = re.compile(
    r"(?ix)(?:"
    r"\b(?:bearer|basic|digest)\s+|"
    r"\bsk-(?:proj-)?[A-Za-z0-9._~+/-]{8,}|"
    r"\b(?:AKIA|ASIA|AIDA|AROA)[A-Z0-9]{16}\b|"
    r"\bAIza[0-9A-Za-z_-]{20,}\b|"
    r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b|"
    r"\bglpat-[A-Za-z0-9_-]{16,}\b|"
    r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b|"
    r"\bsk_(?:live|test)_[A-Za-z0-9]{12,}\b|"
    r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b|"
    r"(?:api[_\-.:]*key|access[_\-.:]*key|account[_\-.:]*key|"
    r"shared[_\-.:]*access[_\-.:]*key|token|secret|"
    r"password|passwd|pwd|sig)\s*[=:]\s*[^\s,;]{4,}|"
    r"[a-z][a-z0-9+.-]*://[^\s/@]*:[^\s/@]+@|"
    r"-----begin\s+(?:rsa\s+|ec\s+|openssh\s+)?private\s+key-----"
    r")"
)
_SAFE_TOKEN_METRIC_RE = re.compile(
    r"(?i)^(?:(?:input|output|cached[_\-.:]*input|reasoning|total|prompt|completion|max)[_\-.:]*)tokens?(?:[_\-.:]*count)?$"
)
_MAX_ATTRIBUTE_BYTES = 8_192
_MAX_EVENT_BYTES = 4_096
_MAX_TEXT = 512


class ObservabilityError(ValueError):
    """Raised when a trace or span violates the WP05 contract."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ObservabilityError(f"{name} must be a non-empty ISO-8601 string")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ObservabilityError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ObservabilityError(f"{name} must include a timezone offset")
    return parsed


def _validate_timestamp(value: Any, name: str) -> str:
    _parse_timestamp(value, name)
    return value


def _trace_id(value: Any) -> str:
    if not isinstance(value, str) or _TRACE_ID_RE.fullmatch(value) is None:
        raise ObservabilityError("trace_id must be 32 lowercase hexadecimal characters")
    if set(value) == {"0"}:
        raise ObservabilityError("trace_id cannot be all zero")
    return value


def _span_id(value: Any, name: str = "span_id") -> str:
    if not isinstance(value, str) or _SPAN_ID_RE.fullmatch(value) is None:
        raise ObservabilityError(f"{name} must be 16 lowercase hexadecimal characters")
    if set(value) == {"0"}:
        raise ObservabilityError(f"{name} cannot be all zero")
    return value


def trace_id_for_run(run_id: str) -> str:
    """Derive a stable OTel-sized trace ID without exposing the run ID."""

    raw = str(run_id).encode("utf-8")
    return hashlib.sha256(b"costweave:trace:" + raw).hexdigest()[:32]


def _redaction_marker(value: Any, *, kind: str) -> dict[str, Any]:
    """Describe removed data without retaining a dictionary-attackable digest."""

    if isinstance(value, (str, bytes, list, tuple, Mapping)):
        length = len(value)
    else:
        length = None
    marker: dict[str, Any] = {
        "redacted": True,
        "kind": kind,
        "value_type": type(value).__name__,
    }
    if length is not None:
        marker["length"] = length
    return marker


def _safe_span_name(value: Any) -> Any:
    if isinstance(value, str) and _SECRET_VALUE_RE.search(value):
        return "redacted.span"
    return value


def _is_secret_key(key_hint: str, value: Any) -> bool:
    if not key_hint:
        return False
    if _SAFE_TOKEN_METRIC_RE.fullmatch(key_hint.strip()):
        is_numeric_metric = (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and (not isinstance(value, float) or math.isfinite(value))
        )
        if is_numeric_metric:
            return False
    return _SECRET_KEY_RE.search(key_hint) is not None


def _is_prompt_key(key_hint: str) -> bool:
    if not key_hint or key_hint.lower() in {"data_residency"}:
        return False
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key_hint)
    parts = [
        item
        for item in re.split(r"[^A-Za-z0-9]+", separated)
        if item
    ]
    return any(_PROMPT_KEY_RE.fullmatch(item) for item in parts)


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _safe_scalar(value: Any, *, key_hint: str = "") -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ObservabilityError("span attributes must contain finite numbers")
    if _is_secret_key(key_hint, value):
        return _redaction_marker(value, kind="secret")
    if _is_prompt_key(key_hint) and not (
        value is None or isinstance(value, (bool, int, float))
    ):
        return _redaction_marker(value, kind="prompt")
    if isinstance(value, (str, bytes)):
        text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
        if _SECRET_VALUE_RE.search(text):
            return _redaction_marker(text, kind="secret")
        if len(text) > _MAX_TEXT:
            return {
                "truncated": True,
                "redacted": True,
                "kind": "long_text",
                "length": len(text),
            }
        return text
    if isinstance(value, float):
        return value
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, Mapping):
        safe_mapping: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            key_text = str(key) if isinstance(key, (str, int, bool)) else ""
            if (
                not key_text
                or _ATTRIBUTE_KEY_RE.fullmatch(key_text) is None
                or _SECRET_VALUE_RE.search(key_text)
            ):
                safe_key = f"redacted_field_{index}"
                item_hint = "secret"
            else:
                safe_key = key_text
                item_hint = key_text
            while safe_key in safe_mapping:
                safe_key = f"{safe_key}_{index}"
            safe_mapping[safe_key] = _safe_scalar(item, key_hint=item_hint)
        return safe_mapping
    if isinstance(value, (list, tuple)):
        return [_safe_scalar(item, key_hint=key_hint) for item in value[:64]]
    return _redaction_marker(value, kind="unsupported_type")


def sanitize_attributes(value: Mapping[str, Any] | None, *, maximum: int = _MAX_ATTRIBUTE_BYTES) -> dict[str, Any]:
    """Return bounded attributes with secrets and prompt-like values removed."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ObservabilityError("span attributes must be an object")
    safe = _safe_scalar(value)
    if not isinstance(safe, dict):
        return {"attributes": safe}
    try:
        raw = json.dumps(
            safe,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ObservabilityError("span attributes must be finite JSON") from exc
    if len(raw.encode("utf-8")) <= max(256, int(maximum)):
        return safe
    return {
        "attributes_truncated": True,
        "bytes": len(raw.encode("utf-8")),
    }


def _redaction_count(value: Any) -> int:
    if isinstance(value, Mapping):
        return int(bool(value.get("redacted"))) + sum(
            _redaction_count(item) for key, item in value.items() if key != "redacted"
        )
    if isinstance(value, (list, tuple)):
        return sum(_redaction_count(item) for item in value)
    return 0


@dataclass(frozen=True, slots=True)
class TracePolicyV1:
    """Safe recording policy; external export is a future, explicit boundary."""

    local_recording_enabled: bool = True
    external_export_enabled: bool = False
    capture_prompts: bool = False
    capture_secrets: bool = False
    max_spans: int = 512
    max_events_per_span: int = 32
    max_attribute_bytes: int = _MAX_ATTRIBUTE_BYTES
    policy_revision: str = OBSERVABILITY_POLICY_REVISION
    schema_version: str = OBSERVABILITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in (
            "local_recording_enabled",
            "external_export_enabled",
            "capture_prompts",
            "capture_secrets",
        ):
            if not isinstance(getattr(self, name), bool):
                raise ObservabilityError(f"{name} must be bool")
        if self.capture_prompts:
            raise ObservabilityError("raw prompt capture is not supported by WP05")
        if self.capture_secrets:
            raise ObservabilityError("secret capture is not supported by WP05")
        if isinstance(self.max_spans, bool) or not 1 <= int(self.max_spans) <= 10_000:
            raise ObservabilityError("max_spans must be between 1 and 10000")
        if isinstance(self.max_events_per_span, bool) or not 0 <= int(self.max_events_per_span) <= 256:
            raise ObservabilityError("max_events_per_span must be between 0 and 256")
        if isinstance(self.max_attribute_bytes, bool) or not 256 <= int(self.max_attribute_bytes) <= 1_000_000:
            raise ObservabilityError("max_attribute_bytes is outside the safe range")
        if self.policy_revision != OBSERVABILITY_POLICY_REVISION:
            raise ObservabilityError(f"policy_revision must be {OBSERVABILITY_POLICY_REVISION}")
        if self.schema_version != OBSERVABILITY_SCHEMA_VERSION:
            raise ObservabilityError(f"schema_version must be {OBSERVABILITY_SCHEMA_VERSION}")

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
            "local_recording_enabled": self.local_recording_enabled,
            "external_export_enabled": self.external_export_enabled,
            "external_export_supported": False,
            "prompt_capture_enabled": False,
            "secret_capture_enabled": False,
            "max_spans": int(self.max_spans),
            "max_events_per_span": int(self.max_events_per_span),
            "max_attribute_bytes": int(self.max_attribute_bytes),
            "exporter": "none",
            "evidence_level": "E0-local-in-memory",
            "network_calls": 0,
            "paid_calls": 0,
        }


@dataclass(frozen=True, slots=True)
class TraceContextV1:
    trace_id: str
    span_id: str
    trace_flags: str = "01"

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_id", _trace_id(self.trace_id))
        object.__setattr__(self, "span_id", _span_id(self.span_id))
        if self.trace_flags not in {"00", "01"}:
            raise ObservabilityError("trace_flags must be 00 or 01")

    @property
    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-{self.trace_flags}"

    def to_dict(self) -> dict[str, str]:
        return {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "trace_flags": self.trace_flags,
            "traceparent": self.traceparent,
        }


@dataclass(frozen=True, slots=True)
class TraceSpanV1:
    trace_id: str
    span_id: str
    parent_span_id: str | None
    name: str
    stage: str
    start_time: str
    end_time: str | None = None
    status: str = "unset"
    kind: str = "internal"
    attributes: Mapping[str, Any] = field(default_factory=dict)
    events: tuple[Mapping[str, Any], ...] = ()
    schema_version: str = OBSERVABILITY_SCHEMA_VERSION
    attribute_limit: int = field(default=_MAX_ATTRIBUTE_BYTES, repr=False, compare=False)
    event_limit: int = field(default=32, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "trace_id", _trace_id(self.trace_id))
        object.__setattr__(self, "span_id", _span_id(self.span_id))
        if self.parent_span_id is not None:
            object.__setattr__(self, "parent_span_id", _span_id(self.parent_span_id, "parent_span_id"))
            if self.parent_span_id == self.span_id:
                raise ObservabilityError("span cannot be its own parent")
        object.__setattr__(self, "name", _safe_span_name(self.name))
        if not isinstance(self.name, str) or _NAME_RE.fullmatch(self.name) is None:
            raise ObservabilityError("span name is invalid")
        if self.stage not in TRACE_STAGES:
            raise ObservabilityError(f"unsupported trace stage: {self.stage!r}")
        if self.kind not in TRACE_KINDS:
            raise ObservabilityError(f"unsupported span kind: {self.kind!r}")
        if self.status not in TRACE_STATUSES:
            raise ObservabilityError(f"unsupported span status: {self.status!r}")
        if isinstance(self.attribute_limit, bool) or not 256 <= int(self.attribute_limit) <= 1_000_000:
            raise ObservabilityError("attribute_limit is outside the safe range")
        if isinstance(self.event_limit, bool) or not 0 <= int(self.event_limit) <= 256:
            raise ObservabilityError("event_limit must be between 0 and 256")
        started = _parse_timestamp(self.start_time, "start_time")
        object.__setattr__(self, "start_time", self.start_time)
        if self.end_time is not None:
            ended = _parse_timestamp(self.end_time, "end_time")
            if ended < started:
                raise ObservabilityError("end_time cannot be earlier than start_time")
            object.__setattr__(self, "end_time", self.end_time)
        object.__setattr__(
            self,
            "attributes",
            _freeze_json(
                sanitize_attributes(
                    self.attributes,
                    maximum=int(self.attribute_limit),
                )
            ),
        )
        safe_events: list[dict[str, Any]] = []
        for event in tuple(self.events)[: int(self.event_limit)]:
            if not isinstance(event, Mapping):
                raise ObservabilityError("span events must be objects")
            timestamp = event.get("timestamp", self.start_time)
            raw_name = event.get("name", "event")
            name = raw_name[:128] if isinstance(raw_name, str) else "event"
            if _SECRET_VALUE_RE.search(name):
                name = "redacted.event"
            event_value = {
                "name": name,
                "timestamp": _validate_timestamp(timestamp, "event.timestamp"),
                "attributes": sanitize_attributes(
                    event.get("attributes") or {},
                    maximum=min(_MAX_EVENT_BYTES, int(self.attribute_limit)),
                ),
            }
            safe_events.append(event_value)
        object.__setattr__(
            self,
            "events",
            tuple(_freeze_json(event) for event in safe_events),
        )
        if self.schema_version != OBSERVABILITY_SCHEMA_VERSION:
            raise ObservabilityError(f"schema_version must be {OBSERVABILITY_SCHEMA_VERSION}")

    @property
    def redaction_count(self) -> int:
        return _redaction_count(self.attributes) + sum(
            _redaction_count(item.get("attributes", {})) for item in self.events
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "stage": self.stage,
            "kind": self.kind,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "status": self.status,
            "attributes": _thaw_json(self.attributes),
            "events": [_thaw_json(item) for item in self.events],
            "redaction_count": self.redaction_count,
        }


class _SpanHandle:
    def __init__(
        self,
        recorder: "TraceRecorder",
        *,
        name: str,
        stage: str,
        kind: str,
        parent_span_id: str,
        attributes: Mapping[str, Any] | None,
    ) -> None:
        self.recorder = recorder
        self.name = _safe_span_name(name)
        self.stage = stage
        self.kind = kind
        self.parent_span_id = parent_span_id
        self.attributes = dict(attributes or {})
        self.started_at = utc_now()
        self.events: list[dict[str, Any]] = []
        self.status = "unset"
        self._finished = False

    def set_status(self, status: str) -> None:
        if status not in TRACE_STATUSES:
            raise ObservabilityError(f"unsupported span status: {status!r}")
        self.status = status

    def add_event(self, name: str, attributes: Mapping[str, Any] | None = None) -> None:
        if len(self.events) >= self.recorder.policy.max_events_per_span:
            return
        self.events.append({
            "name": name[:128] if isinstance(name, str) else "event",
            "timestamp": utc_now(),
            "attributes": dict(attributes or {}),
        })

    def finish(self, status: str | None = None) -> TraceSpanV1 | None:
        if self._finished:
            return None
        self._finished = True
        if status is not None:
            self.set_status(status)
        if self.status == "unset":
            self.status = "ok"
        return self.recorder._append_span(
            TraceSpanV1(
                trace_id=self.recorder.trace_id,
                span_id=self.recorder._next_span_id(self.name),
                parent_span_id=self.parent_span_id,
                name=self.name,
                stage=self.stage,
                kind=self.kind,
                start_time=self.started_at,
                end_time=utc_now(),
                status=self.status,
                attributes=self.attributes,
                events=tuple(self.events),
                attribute_limit=int(self.recorder.policy.max_attribute_bytes),
                event_limit=int(self.recorder.policy.max_events_per_span),
            )
        )

    def __enter__(self) -> "_SpanHandle":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        self.finish("error" if exc_type is not None else None)
        return False


class TraceRecorder:
    """Thread-safe bounded trace recorder with no network side effects."""

    def __init__(
        self,
        *,
        trace_id: str | None = None,
        policy: TracePolicyV1 | None = None,
        root_start_time: str | None = None,
        root_end_time: str | None = None,
    ) -> None:
        self.policy = policy or TracePolicyV1()
        self.trace_id = _trace_id(trace_id or uuid.uuid4().hex)
        self.root_span_id = self.trace_id[:16]
        self._lock = threading.RLock()
        self._spans: list[TraceSpanV1] = []
        self._dropped = 0
        self._sequence = 0
        root_started = root_start_time or utc_now()
        root_started_value = _parse_timestamp(root_started, "root_start_time")
        if root_end_time is not None:
            root_ended_value = _parse_timestamp(root_end_time, "root_end_time")
            if root_ended_value < root_started_value:
                raise ObservabilityError(
                    "root_end_time cannot be earlier than root_start_time"
                )
        if self.policy.local_recording_enabled:
            self._spans.append(
                TraceSpanV1(
                    trace_id=self.trace_id,
                    span_id=self.root_span_id,
                    parent_span_id=None,
                    name="costweave.goal",
                    stage="goal",
                    kind="internal",
                    start_time=root_started,
                    end_time=root_end_time,
                    status="unset",
                    attributes={"trace_origin": "costweave"},
                    attribute_limit=int(self.policy.max_attribute_bytes),
                    event_limit=int(self.policy.max_events_per_span),
                )
            )

    def _next_span_id(self, seed: str) -> str:
        with self._lock:
            self._sequence += 1
            safe_seed = _safe_span_name(seed)
            raw = f"{self.trace_id}:{self._sequence}:{safe_seed}".encode("utf-8")
            return hashlib.sha256(raw).hexdigest()[:16]

    def _append_span(self, span: TraceSpanV1) -> TraceSpanV1 | None:
        with self._lock:
            if not self.policy.local_recording_enabled:
                return None
            if len(self._spans) >= int(self.policy.max_spans):
                self._dropped += 1
                return None
            self._spans.append(span)
            return span

    def start_span(
        self,
        name: str,
        *,
        stage: str,
        kind: str = "internal",
        parent_span_id: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> _SpanHandle:
        if stage not in TRACE_STAGES:
            raise ObservabilityError(f"unsupported trace stage: {stage!r}")
        parent = parent_span_id or self.root_span_id
        _span_id(parent, "parent_span_id")
        return _SpanHandle(
            self,
            name=name,
            stage=stage,
            kind=kind,
            parent_span_id=parent,
            attributes=attributes,
        )

    def record_stage(
        self,
        stage: str,
        *,
        attributes: Mapping[str, Any] | None = None,
        parent_span_id: str | None = None,
        start_time: str | None = None,
        end_time: str | None = None,
        status: str = "ok",
        name: str | None = None,
        kind: str = "internal",
    ) -> TraceSpanV1 | None:
        if stage not in TRACE_STAGES:
            raise ObservabilityError(f"unsupported trace stage: {stage!r}")
        started = start_time or utc_now()
        ended = end_time or started
        span = TraceSpanV1(
            trace_id=self.trace_id,
            span_id=self._next_span_id(name or f"costweave.{stage}"),
            parent_span_id=parent_span_id or self.root_span_id,
            name=name or f"costweave.{stage}",
            stage=stage,
            kind=kind,
            start_time=started,
            end_time=ended,
            status=status,
            attributes=attributes or {},
            attribute_limit=int(self.policy.max_attribute_bytes),
            event_limit=int(self.policy.max_events_per_span),
        )
        return self._append_span(span)

    def record_provider_call(
        self,
        *,
        backend_id: str,
        provider: str,
        model: str,
        request_id: str,
        status: str = "ok",
        attributes: Mapping[str, Any] | None = None,
    ) -> TraceSpanV1 | None:
        values = {
            "backend_id": backend_id,
            "provider": provider,
            "model": model,
            "request_id": request_id,
            **dict(attributes or {}),
        }
        return self.record_stage(
            "provider_call",
            name="costweave.provider.call",
            kind="client",
            status=status,
            attributes=values,
        )

    @contextmanager
    def provider_call_span(
        self,
        *,
        backend_id: str,
        provider: str,
        model: str,
        request_id: str,
        attributes: Mapping[str, Any] | None = None,
    ) -> Iterator[_SpanHandle | None]:
        if not self.policy.local_recording_enabled:
            yield None
            return
        with self.start_span(
            "costweave.provider.call",
            stage="provider_call",
            kind="client",
            attributes={
                "backend_id": backend_id,
                "provider": provider,
                "model": model,
                "request_id": request_id,
                **dict(attributes or {}),
            },
        ) as handle:
            yield handle

    def record_event(
        self,
        event_kind: str,
        *,
        task_id: str | None = None,
        occurred_at: str | None = None,
        attributes: Mapping[str, Any] | None = None,
    ) -> TraceSpanV1 | None:
        text = str(event_kind)
        if text in {"planning", "planning_mode_decided", "planning_deliberated", "replanning", "replan_validated", "plan_validated"}:
            stage = "plan"
        elif "validat" in text or text in {"node_validated", "node_validation_failed"}:
            stage = "validation"
        elif "recover" in text or text in {"pre_recovery", "recovery_stopped", "replanning"}:
            stage = "recovery"
        elif text.startswith("task") or text.startswith("node") or text.startswith("artifact"):
            stage = "task"
        else:
            stage = "goal"
        values = {"event_kind": text}
        if task_id is not None:
            values["task_id"] = str(task_id)
        values.update(dict(attributes or {}))
        if _SECRET_VALUE_RE.search(text):
            safe_name = "redacted.event"
        else:
            safe_name = re.sub(r"[^a-z0-9_.:-]", ".", text.lower())[:110]
            if not safe_name or not safe_name[0].isalpha():
                safe_name = "event." + safe_name
        lowered = text.lower()
        status = "error" if any(
            marker in lowered
            for marker in (
                "failed",
                "failure",
                "rejected",
                "invalidated",
                "error",
                "timeout",
                "timed_out",
                "cancelled",
                "canceled",
                "recovery_stopped",
            )
        ) else "ok"
        return self.record_stage(
            stage,
            name=f"costweave.{safe_name}",
            attributes=values,
            start_time=occurred_at,
            end_time=occurred_at,
            status=status,
        )

    def replay_events(self, events: Any) -> None:
        for event in tuple(events or ()):
            try:
                self.record_event(
                    getattr(event, "kind", "runtime.event"),
                    task_id=getattr(event, "task_id", None),
                    occurred_at=getattr(event, "at", None),
                    attributes=getattr(event, "detail", None),
                )
            except (ObservabilityError, TypeError, ValueError):
                with self._lock:
                    self._dropped += 1

    def finish_root(self, status: str = "ok") -> None:
        if status not in TRACE_STATUSES or status == "unset":
            raise ObservabilityError("root status must be ok or error")
        with self._lock:
            for index, span in enumerate(self._spans):
                if span.span_id == self.root_span_id:
                    self._spans[index] = replace(span, end_time=span.end_time or utc_now(), status=status)
                    return

    def spans(self) -> tuple[TraceSpanV1, ...]:
        with self._lock:
            return tuple(self._spans)

    @staticmethod
    def _unix_nanos(timestamp: str | None) -> str | None:
        if timestamp is None:
            return None
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        return str(int(value.timestamp() * 1_000_000_000))

    @staticmethod
    def _otel_value(value: Any) -> dict[str, Any]:
        if isinstance(value, bool):
            return {"boolValue": value}
        if isinstance(value, int) and not isinstance(value, bool):
            return {"intValue": str(value)}
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ObservabilityError("OTel attributes must contain finite numbers")
            return {"doubleValue": value}
        if isinstance(value, str):
            return {"stringValue": value}
        return {
            "stringValue": json.dumps(
                _thaw_json(value),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        }

    def export(self) -> dict[str, Any]:
        spans = [item.to_dict() for item in self.spans()]
        return {
            "schema_version": OBSERVABILITY_SCHEMA_VERSION,
            "policy_revision": OBSERVABILITY_POLICY_REVISION,
            "trace_id": self.trace_id,
            "traceparent": f"00-{self.trace_id}-{self.root_span_id}-01",
            "root_span_id": self.root_span_id,
            "policy": self.policy.public(),
            "spans": spans,
            "span_count": len(spans),
            "dropped_span_count": self._dropped,
            "redaction_count": sum(int(item.get("redaction_count", 0)) for item in spans),
            "evidence_level": "E0-local-in-memory",
            "external_exported": False,
        }

    def to_otel(self) -> dict[str, Any]:
        otel_spans: list[dict[str, Any]] = []
        for span in self.spans():
            attributes = [
                {"key": str(key), "value": self._otel_value(value)}
                for key, value in span.attributes.items()
            ]
            item: dict[str, Any] = {
                "traceId": span.trace_id,
                "spanId": span.span_id,
                "parentSpanId": span.parent_span_id or "",
                "name": span.name,
                "kind": {"internal": 1, "server": 2, "client": 3, "producer": 4, "consumer": 5}[span.kind],
                "startTimeUnixNano": self._unix_nanos(span.start_time),
                "attributes": attributes,
                "status": {"code": {"unset": 0, "ok": 1, "error": 2}[span.status]},
            }
            if span.end_time is not None:
                item["endTimeUnixNano"] = self._unix_nanos(span.end_time)
            otel_spans.append(item)
        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": [
                            {"key": "service.name", "value": {"stringValue": "costweave"}},
                            {"key": "service.version", "value": {"stringValue": "0.6.6"}},
                        ]
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "costweave.observability", "version": OBSERVABILITY_SCHEMA_VERSION},
                            "spans": otel_spans,
                        }
                    ],
                }
            ]
        }

    def summary(self) -> dict[str, Any]:
        spans = self.spans()
        counts = {stage: 0 for stage in TRACE_STAGES}
        for span in spans:
            counts[span.stage] = counts.get(span.stage, 0) + 1
        return {
            "schema_version": OBSERVABILITY_SCHEMA_VERSION,
            "policy_revision": OBSERVABILITY_POLICY_REVISION,
            "trace_id": self.trace_id,
            "root_span_id": self.root_span_id,
            "span_count": len(spans),
            "dropped_span_count": self._dropped,
            "redaction_count": sum(span.redaction_count for span in spans),
            "stage_counts": counts,
            "evidence_level": "E0-local-in-memory",
            "external_exported": False,
        }


_REGISTRY_LOCK = threading.RLock()
_REGISTRY: "weakref.WeakValueDictionary[str, TraceRecorder]" = (
    weakref.WeakValueDictionary()
)


def register_trace_recorder(recorder: TraceRecorder) -> None:
    if not isinstance(recorder, TraceRecorder):
        raise TypeError("recorder must be a TraceRecorder")
    with _REGISTRY_LOCK:
        _REGISTRY[recorder.trace_id] = recorder


def unregister_trace_recorder(trace_id: str) -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.pop(str(trace_id), None)


def get_trace_recorder(trace_id: str) -> TraceRecorder | None:
    with _REGISTRY_LOCK:
        return _REGISTRY.get(str(trace_id))


@contextmanager
def provider_call_span(
    trace_id: str,
    *,
    backend_id: str,
    provider: str,
    model: str,
    request_id: str,
    attributes: Mapping[str, Any] | None = None,
) -> Iterator[_SpanHandle | None]:
    recorder = get_trace_recorder(trace_id)
    if recorder is None:
        yield None
        return
    with recorder.provider_call_span(
        backend_id=backend_id,
        provider=provider,
        model=model,
        request_id=request_id,
        attributes=attributes,
    ) as handle:
        yield handle


def public_observability_policy() -> dict[str, Any]:
    return TracePolicyV1().public()


__all__ = [
    "OBSERVABILITY_SCHEMA_VERSION",
    "OBSERVABILITY_POLICY_REVISION",
    "TRACE_STAGES",
    "TraceContextV1",
    "TracePolicyV1",
    "TraceRecorder",
    "TraceSpanV1",
    "ObservabilityError",
    "get_trace_recorder",
    "provider_call_span",
    "public_observability_policy",
    "register_trace_recorder",
    "sanitize_attributes",
    "trace_id_for_run",
    "unregister_trace_recorder",
]
