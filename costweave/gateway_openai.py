"""OpenAI-compatible local Gateway Backend for v0.6.6 V066-WP02.

The module is deliberately local-only.  It maps the provider-neutral gateway
contract to the common ``/v1/chat/completions`` wire shape, but it does not
enable a network call unless a caller explicitly supplies a policy with
``network_enabled=True``.  The in-process fake server is used by contract
tests and never opens a socket.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Protocol
from urllib.parse import urlparse

from .gateway_backend import (
    GATEWAY_SCHEMA_VERSION,
    GatewayBackend,
    GatewayBackendContractError,
    GatewayBackendDisabledError,
    GatewayBillingV1,
    GatewayCapabilitiesV1,
    GatewayCancellationV1,
    GatewayErrorV1,
    GatewayRequestV1,
    GatewayResponseV1,
    GatewayStreamEventV1,
    GatewayToolV1,
    GatewayUsageV1,
)
from .security_boundary import validate_endpoint
from .observability import provider_call_span


OPENAI_GATEWAY_REVISION = "openai-compatible-gateway-v1"
OPENAI_GATEWAY_POLICY_REVISION = "local-openai-endpoint-v1"
OPENAI_GATEWAY_DEPLOYMENT = "local"
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,191}$")
_MAX_RESPONSE_BYTES = 4_000_000
_MAX_REQUEST_BYTES = 2_000_000


class OpenAICompatibleGatewayError(GatewayBackendContractError):
    """Raised when the local OpenAI-compatible boundary is invalid."""


class OpenAICompatibleTransportError(OpenAICompatibleGatewayError):
    """Raised when an explicitly enabled local transport cannot complete."""


@dataclass(frozen=True, slots=True)
class OpenAICompatibleGatewayConfig:
    """Explicit local endpoint configuration.

    Remote endpoints are intentionally not representable in WP02.  Remote
    and paid provider support belongs to later work packages with separate
    authorization and reconciliation gates.
    """

    backend_id: str
    endpoint: str
    timeout_seconds: float = 30.0
    max_response_bytes: int = _MAX_RESPONSE_BYTES
    deployment: str = OPENAI_GATEWAY_DEPLOYMENT
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.backend_id, str) or _IDENTIFIER_RE.fullmatch(self.backend_id) is None:
            raise OpenAICompatibleGatewayError("backend_id is invalid")
        if self.deployment != OPENAI_GATEWAY_DEPLOYMENT:
            raise OpenAICompatibleGatewayError("WP02 only supports local deployment")
        try:
            validate_endpoint(self.endpoint, deployment="local")
        except ValueError as exc:
            raise OpenAICompatibleGatewayError(str(exc)) from exc
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, (int, float)):
            raise OpenAICompatibleGatewayError("timeout_seconds must be numeric")
        if self.timeout_seconds <= 0 or self.timeout_seconds > 300:
            raise OpenAICompatibleGatewayError("timeout_seconds must be between 0 and 300")
        if isinstance(self.max_response_bytes, bool) or not isinstance(self.max_response_bytes, int):
            raise OpenAICompatibleGatewayError("max_response_bytes must be an int")
        if not 1 <= self.max_response_bytes <= _MAX_RESPONSE_BYTES:
            raise OpenAICompatibleGatewayError("max_response_bytes is outside the safe bound")
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise OpenAICompatibleGatewayError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "endpoint": self.endpoint,
            "timeout_seconds": float(self.timeout_seconds),
            "max_response_bytes": self.max_response_bytes,
            "deployment": self.deployment,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class OpenAICompatibleRuntimePolicy:
    """Explicit admission switch for a local endpoint."""

    network_enabled: bool = False
    policy_revision: str = OPENAI_GATEWAY_POLICY_REVISION

    def __post_init__(self) -> None:
        if not isinstance(self.network_enabled, bool):
            raise OpenAICompatibleGatewayError("network_enabled must be bool")
        if self.policy_revision != OPENAI_GATEWAY_POLICY_REVISION:
            raise OpenAICompatibleGatewayError(
                f"policy_revision must be {OPENAI_GATEWAY_POLICY_REVISION}"
            )

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": GATEWAY_SCHEMA_VERSION,
            "policy_revision": self.policy_revision,
            "network_enabled": self.network_enabled,
            "local_endpoint_only": True,
            "remote_endpoint_allowed": False,
            "paid_calls_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class GatewayHTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]
    elapsed_ms: int

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int):
            raise OpenAICompatibleTransportError("status_code must be an int")
        if not 100 <= self.status_code <= 599:
            raise OpenAICompatibleTransportError("status_code is invalid")
        if not isinstance(self.body, bytes):
            raise OpenAICompatibleTransportError("response body must be bytes")
        if isinstance(self.elapsed_ms, bool) or not isinstance(self.elapsed_ms, int) or self.elapsed_ms < 0:
            raise OpenAICompatibleTransportError("elapsed_ms is invalid")
        if not isinstance(self.headers, Mapping):
            raise OpenAICompatibleTransportError("response headers must be an object")
        object.__setattr__(self, "headers", {str(key): str(value) for key, value in self.headers.items()})


class GatewayHTTPTransport(Protocol):
    evidence_level: str

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> GatewayHTTPResponse:
        ...


class LocalGatewayHTTPTransport:
    """Minimal local-only urllib transport.

    This class is inert until the backend policy admits it.  Redirects are
    rejected and the endpoint is validated again immediately before the
    socket operation.
    """

    evidence_level = "E0-local-transport"

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> GatewayHTTPResponse:
        try:
            validate_endpoint(url, deployment="local")
        except ValueError as exc:
            raise OpenAICompatibleTransportError(str(exc)) from exc
        request = urllib.request.Request(
            url,
            data=body,
            headers=dict(headers),
            method="POST",
        )
        started = time.monotonic()
        opener = urllib.request.build_opener(self._NoRedirect())
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                payload = response.read(max_response_bytes + 1)
                if len(payload) > max_response_bytes:
                    raise OpenAICompatibleTransportError("response_too_large")
                return GatewayHTTPResponse(
                    status_code=int(response.status),
                    body=payload,
                    headers=dict(response.headers.items()),
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
        except urllib.error.HTTPError as exc:
            try:
                payload = exc.read(max_response_bytes + 1)
                if len(payload) > max_response_bytes:
                    raise OpenAICompatibleTransportError("response_too_large")
                return GatewayHTTPResponse(
                    status_code=int(exc.code),
                    body=payload,
                    headers=dict(exc.headers.items()),
                    elapsed_ms=round((time.monotonic() - started) * 1000),
                )
            finally:
                exc.close()
        except OpenAICompatibleTransportError:
            raise
        except Exception as exc:
            raise OpenAICompatibleTransportError("local_transport_error") from exc


class FakeOpenAICompatibleServer:
    """In-process fake server used for wire-contract tests.

    It implements the same transport surface as the local HTTP transport but
    never opens a socket.  The recorded request list is test evidence only and
    must not be used as production telemetry.
    """

    evidence_level = "E0-fake-server"

    def __init__(
        self,
        *,
        content: str = "fake local response",
        provider_request_id: str = "fake-request-001",
        input_tokens: int = 11,
        output_tokens: int = 5,
        cached_input_tokens: int = 2,
        status_code: int = 200,
    ) -> None:
        self.content = content
        self.provider_request_id = provider_request_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_input_tokens = cached_input_tokens
        self.status_code = status_code
        self.requests: list[dict[str, Any]] = []

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        max_response_bytes: int,
    ) -> GatewayHTTPResponse:
        del url, timeout_seconds
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenAICompatibleTransportError("fake request is not JSON") from exc
        if not isinstance(payload, Mapping):
            raise OpenAICompatibleTransportError("fake request must be an object")
        self.requests.append({"headers": dict(headers), "payload": dict(payload)})
        usage = {
            "prompt_tokens": self.input_tokens,
            "completion_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "prompt_tokens_details": {"cached_tokens": self.cached_input_tokens},
        }
        provider_id = self.provider_request_id
        if payload.get("stream"):
            chunks = (
                {
                    "id": provider_id,
                    "choices": [{"delta": {"role": "assistant", "content": self.content}, "finish_reason": None}],
                },
                {
                    "id": provider_id,
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": usage,
                },
            )
            raw = "".join(f"data: {json.dumps(item, ensure_ascii=False)}\n\n" for item in chunks) + "data: [DONE]\n\n"
            body_out = raw.encode("utf-8")
        else:
            body_out = json.dumps(
                {
                    "id": provider_id,
                    "object": "chat.completion",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": self.content}, "finish_reason": "stop"}],
                    "usage": usage,
                },
                ensure_ascii=False,
            ).encode("utf-8")
        if len(body_out) > max_response_bytes:
            raise OpenAICompatibleTransportError("fake response is too large")
        return GatewayHTTPResponse(
            status_code=self.status_code,
            body=body_out,
            headers={"Content-Type": "application/json", "X-Request-ID": provider_id},
            elapsed_ms=1,
        )


def _provider_id(value: Any) -> str | None:
    if isinstance(value, str) and _IDENTIFIER_RE.fullmatch(value):
        return value
    return None


def _safe_message(value: Any, fallback: str) -> str:
    if isinstance(value, str) and value.strip():
        # Do not return provider error text: it may contain a URL, credential
        # fragment or prompt supplied by an untrusted endpoint.
        return fallback
    return fallback


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OpenAICompatibleGatewayError(f"{name} must be a non-negative int")
    return value


def _usage(value: Any) -> GatewayUsageV1 | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise OpenAICompatibleGatewayError("provider usage is invalid")
    input_tokens = _integer(value.get("prompt_tokens", value.get("input_tokens", 0)), "input_tokens")
    output_tokens = _integer(value.get("completion_tokens", value.get("output_tokens", 0)), "output_tokens")
    prompt_details = value.get("prompt_tokens_details") or value.get("input_tokens_details") or {}
    completion_details = value.get("completion_tokens_details") or value.get("output_tokens_details") or {}
    if not isinstance(prompt_details, Mapping) or not isinstance(completion_details, Mapping):
        raise OpenAICompatibleGatewayError("provider usage details are invalid")
    cached = _integer(prompt_details.get("cached_tokens", value.get("cached_input_tokens", 0)), "cached_input_tokens")
    reasoning = _integer(completion_details.get("reasoning_tokens", value.get("reasoning_tokens", 0)), "reasoning_tokens")
    return GatewayUsageV1(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_input_tokens=cached,
        reasoning_tokens=reasoning,
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _billing(headers: Mapping[str, str]) -> GatewayBillingV1:
    raw = _header(headers, "x-cost-usd") or _header(headers, "x-billed-usd")
    if raw is None:
        return GatewayBillingV1()
    try:
        cost = float(raw)
    except (TypeError, ValueError) as exc:
        raise OpenAICompatibleGatewayError("provider billing header is invalid") from exc
    if cost < 0:
        raise OpenAICompatibleGatewayError("provider billing header is negative")
    return GatewayBillingV1(source="provider", total_cost_usd=cost, reconciled=True)


def _error_category(status_code: int) -> tuple[str, bool]:
    if status_code in {401}:
        return "authentication", False
    if status_code in {403}:
        return "authorization", False
    if status_code in {408, 504}:
        return "timeout", True
    if status_code in {429}:
        return "rate_limit", True
    if status_code in {500, 502, 503}:
        return "overloaded", True
    return "provider", False


def _response_format_output(request: GatewayRequestV1, content: str) -> Mapping[str, Any] | list[Any] | None:
    if not request.response_format or not isinstance(content, str):
        return None
    format_type = request.response_format.get("type")
    if format_type not in {"json_object", "json_schema"}:
        return None
    try:
        value = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, (Mapping, list)) else None


def _openai_tools(tools: Iterable[GatewayToolV1]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": dict(tool.input_schema),
            },
        }
        for tool in tools
    ]


def _openai_messages(request: GatewayRequestV1) -> list[dict[str, Any]]:
    """Strip CostWeave-only schema fields before crossing the provider wire."""

    messages: list[dict[str, Any]] = []
    for item in request.messages:
        message: dict[str, Any] = {"role": item.role, "content": item.content}
        if item.name is not None:
            message["name"] = item.name
        messages.append(message)
    return messages


class OpenAICompatibleGatewayBackend:
    """GatewayBackend implementation for an explicitly enabled local endpoint."""

    def __init__(
        self,
        config: OpenAICompatibleGatewayConfig,
        *,
        policy: OpenAICompatibleRuntimePolicy | None = None,
        transport: GatewayHTTPTransport | None = None,
    ) -> None:
        self.config = config
        self.policy = policy or OpenAICompatibleRuntimePolicy()
        self.transport = transport or LocalGatewayHTTPTransport()
        self._cancelled: set[str] = set()
        self._capabilities = GatewayCapabilitiesV1(
            backend_id=config.backend_id,
            protocol="openai-compatible",
            deployment="local",
            modalities=("text",),
            supports_streaming=True,
            supports_cancellation=False,
            supports_structured_output=True,
            supports_tools=True,
            reports_usage=True,
            reports_billing=False,
            max_context_tokens=0,
            max_output_tokens=0,
        )

    @property
    def capabilities(self) -> GatewayCapabilitiesV1:
        return self._capabilities

    def _admit(self) -> None:
        if not self.policy.network_enabled:
            raise GatewayBackendDisabledError(
                "local OpenAI-compatible endpoint is disabled by policy"
            )

    def _payload(self, request: GatewayRequestV1) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": request.model,
            "messages": _openai_messages(request),
            "max_tokens": request.max_output_tokens,
            "stream": request.stream,
        }
        if request.tools:
            payload["tools"] = _openai_tools(request.tools)
        if request.response_format is not None:
            payload["response_format"] = dict(request.response_format)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > _MAX_REQUEST_BYTES:
            raise OpenAICompatibleGatewayError("gateway request exceeds safe size")
        return payload

    def _headers(self, request: GatewayRequestV1) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "text/event-stream" if request.stream else "application/json",
            "Idempotency-Key": request.idempotency_key,
            "X-CostWeave-Request-ID": request.request_id,
            "X-CostWeave-Trace-ID": request.trace_id,
        }

    def _failure(
        self,
        request: GatewayRequestV1,
        *,
        category: str,
        retryable: bool,
        message: str,
        provider_code: str | None = None,
        provider_request_id: str | None = None,
    ) -> GatewayResponseV1:
        return GatewayResponseV1(
            request_id=request.request_id,
            status="failed",
            provider_request_id=_provider_id(provider_request_id),
            error=GatewayErrorV1(
                category=category,
                message=_safe_message(message, message),
                retryable=retryable,
                provider_code=_provider_id(provider_code),
                provider_request_id=_provider_id(provider_request_id),
            ),
        )

    def _post(self, request: GatewayRequestV1) -> GatewayHTTPResponse:
        payload = self._payload(request)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return self.transport.post(
            url=self.config.endpoint,
            headers=self._headers(request),
            body=raw,
            timeout_seconds=float(self.config.timeout_seconds),
            max_response_bytes=self.config.max_response_bytes,
        )

    def _completed(self, request: GatewayRequestV1, response: GatewayHTTPResponse) -> GatewayResponseV1:
        provider_request_id = _provider_id(_header(response.headers, "x-request-id"))
        if not 200 <= response.status_code < 300:
            category, retryable = _error_category(response.status_code)
            code = None
            try:
                error_value = json.loads(response.body.decode("utf-8"))
                if isinstance(error_value, Mapping):
                    error_object = error_value.get("error")
                    if isinstance(error_object, Mapping):
                        code = _provider_id(error_object.get("code"))
                    provider_request_id = _provider_id(error_value.get("id")) or provider_request_id
            except (UnicodeDecodeError, json.JSONDecodeError):
                pass
            return self._failure(
                request,
                category=category,
                retryable=retryable,
                message=f"OpenAI-compatible provider returned HTTP {response.status_code}",
                provider_code=code,
                provider_request_id=provider_request_id,
            )
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return self._failure(
                request,
                category="protocol",
                retryable=False,
                message="OpenAI-compatible response was not valid JSON",
                provider_request_id=provider_request_id,
            )
        if not isinstance(value, Mapping):
            return self._failure(
                request,
                category="protocol",
                retryable=False,
                message="OpenAI-compatible response must be an object",
                provider_request_id=provider_request_id,
            )
        provider_request_id = _provider_id(value.get("id")) or provider_request_id
        try:
            choices = value.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                raise OpenAICompatibleGatewayError("choices are missing")
            message = choices[0].get("message")
            if not isinstance(message, Mapping):
                raise OpenAICompatibleGatewayError("message is missing")
            content = message.get("content", "")
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise OpenAICompatibleGatewayError("text content is invalid")
            tool_calls = message.get("tool_calls") or ()
            if not isinstance(tool_calls, (list, tuple)) or any(not isinstance(item, Mapping) for item in tool_calls):
                raise OpenAICompatibleGatewayError("tool calls are invalid")
            usage = _usage(value.get("usage"))
            billing = _billing(response.headers)
            return GatewayResponseV1(
                request_id=request.request_id,
                status="completed",
                content=content,
                structured_output=_response_format_output(request, content),
                tool_calls=tuple(dict(item) for item in tool_calls),
                usage=usage,
                billing=billing,
                provider_request_id=provider_request_id,
            )
        except (OpenAICompatibleGatewayError, GatewayBackendContractError):
            return self._failure(
                request,
                category="protocol",
                retryable=False,
                message="OpenAI-compatible response violated the gateway contract",
                provider_request_id=provider_request_id,
            )

    def submit(self, request: GatewayRequestV1) -> GatewayResponseV1:
        self._admit()
        if request.stream:
            raise OpenAICompatibleGatewayError("streaming request must use stream()")
        with provider_call_span(
            request.trace_id,
            backend_id=self.config.backend_id,
            provider="openai-compatible",
            model=request.model,
            request_id=request.request_id,
        ) as trace_span:
            try:
                response = self._completed(request, self._post(request))
                if trace_span is not None:
                    trace_span.set_status("ok" if response.status == "completed" else "error")
                return response
            except GatewayBackendDisabledError:
                raise
            except OpenAICompatibleTransportError:
                if trace_span is not None:
                    trace_span.set_status("error")
                return self._failure(
                    request,
                    category="network",
                    retryable=True,
                    message="local OpenAI-compatible transport failed",
                )

    def _stream_events(self, request: GatewayRequestV1, response: GatewayHTTPResponse) -> tuple[GatewayStreamEventV1, ...]:
        events: list[GatewayStreamEventV1] = [
            GatewayStreamEventV1(
                request_id=request.request_id,
                sequence=0,
                event_type="started",
                payload={"backend_id": self.config.backend_id},
            )
        ]
        sequence = 1
        provider_request_id: str | None = None
        completed = False
        if not 200 <= response.status_code < 300:
            category, retryable = _error_category(response.status_code)
            return (
                events[0],
                GatewayStreamEventV1(
                    request_id=request.request_id,
                    sequence=1,
                    event_type="error",
                    payload={
                        "category": category,
                        "retryable": retryable,
                        "message": "OpenAI-compatible provider returned an error",
                    },
                ),
            )
        try:
            lines = response.body.decode("utf-8").splitlines()
        except UnicodeDecodeError:
            lines = []
        for line in lines:
            if request.request_id in self._cancelled:
                events.append(
                    GatewayStreamEventV1(
                        request_id=request.request_id,
                        sequence=sequence,
                        event_type="cancelled",
                        payload={"reason": "cancelled before provider completion"},
                        provider_request_id=provider_request_id,
                    )
                )
                return tuple(events)
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                events.append(
                    GatewayStreamEventV1(
                        request_id=request.request_id,
                        sequence=sequence,
                        event_type="completed",
                        payload={},
                        provider_request_id=provider_request_id,
                    )
                )
                completed = True
                sequence += 1
                continue
            try:
                value = json.loads(data)
            except (TypeError, json.JSONDecodeError):
                events.append(
                    GatewayStreamEventV1(
                        request_id=request.request_id,
                        sequence=sequence,
                        event_type="error",
                        payload={"category": "protocol", "message": "invalid stream event"},
                        provider_request_id=provider_request_id,
                    )
                )
                sequence += 1
                continue
            if not isinstance(value, Mapping):
                continue
            provider_request_id = _provider_id(value.get("id")) or provider_request_id
            choices = value.get("choices")
            if isinstance(choices, list) and choices and isinstance(choices[0], Mapping):
                delta = choices[0].get("delta")
                if isinstance(delta, Mapping) and isinstance(delta.get("content"), str) and delta["content"]:
                    events.append(
                        GatewayStreamEventV1(
                            request_id=request.request_id,
                            sequence=sequence,
                            event_type="delta",
                            payload={"text": delta["content"]},
                            provider_request_id=provider_request_id,
                        )
                    )
                    sequence += 1
                finish_reason = choices[0].get("finish_reason")
                if finish_reason is not None and finish_reason != "stop":
                    events.append(
                        GatewayStreamEventV1(
                            request_id=request.request_id,
                            sequence=sequence,
                            event_type="completed",
                            payload={"finish_reason": str(finish_reason)},
                            provider_request_id=provider_request_id,
                        )
                    )
                    completed = True
                    sequence += 1
            if value.get("usage") is not None:
                try:
                    usage = _usage(value.get("usage"))
                    usage_payload = {} if usage is None else usage.to_dict()
                except (OpenAICompatibleGatewayError, GatewayBackendContractError):
                    usage_payload = {"error": "invalid_usage"}
                events.append(
                    GatewayStreamEventV1(
                        request_id=request.request_id,
                        sequence=sequence,
                        event_type="usage",
                        payload=usage_payload,
                        provider_request_id=provider_request_id,
                    )
                )
                sequence += 1
        if not completed:
            events.append(
                GatewayStreamEventV1(
                    request_id=request.request_id,
                    sequence=sequence,
                    event_type="completed",
                    payload={},
                    provider_request_id=provider_request_id,
                )
            )
        return tuple(events)

    def stream(self, request: GatewayRequestV1) -> Iterable[GatewayStreamEventV1]:
        self._admit()
        if not request.stream:
            raise OpenAICompatibleGatewayError("stream() requires request.stream=True")
        if request.request_id in self._cancelled:
            yield GatewayStreamEventV1(
                request_id=request.request_id,
                sequence=0,
                event_type="cancelled",
                payload={"reason": "cancelled before provider request"},
            )
            return
        with provider_call_span(
            request.trace_id,
            backend_id=self.config.backend_id,
            provider="openai-compatible",
            model=request.model,
            request_id=request.request_id,
        ) as trace_span:
            try:
                response = self._post(request)
            except OpenAICompatibleTransportError:
                if trace_span is not None:
                    trace_span.set_status("error")
                yield GatewayStreamEventV1(
                    request_id=request.request_id,
                    sequence=0,
                    event_type="error",
                    payload={"category": "network", "message": "local transport failed"},
                )
                return
            failed = False
            for event in self._stream_events(request, response):
                if event.event_type in {"error", "cancelled"}:
                    failed = True
                yield event
            if trace_span is not None:
                trace_span.set_status("error" if failed else "ok")

    def cancel(self, request_id: str, *, reason: str) -> GatewayCancellationV1:
        if not isinstance(request_id, str) or _IDENTIFIER_RE.fullmatch(request_id) is None:
            raise OpenAICompatibleGatewayError("request_id is invalid")
        if not isinstance(reason, str) or not reason.strip():
            raise OpenAICompatibleGatewayError("cancellation reason is required")
        self._cancelled.add(request_id)
        return GatewayCancellationV1(
            request_id=request_id,
            accepted=False,
            reason="provider-specific cancellation is not supported by WP02",
        )


def public_openai_gateway_policy() -> dict[str, Any]:
    return OpenAICompatibleRuntimePolicy().public()


__all__ = [
    "FakeOpenAICompatibleServer",
    "GatewayHTTPResponse",
    "GatewayHTTPTransport",
    "LocalGatewayHTTPTransport",
    "OPENAI_GATEWAY_DEPLOYMENT",
    "OPENAI_GATEWAY_POLICY_REVISION",
    "OPENAI_GATEWAY_REVISION",
    "OpenAICompatibleGatewayBackend",
    "OpenAICompatibleGatewayConfig",
    "OpenAICompatibleGatewayError",
    "OpenAICompatibleRuntimePolicy",
    "OpenAICompatibleTransportError",
    "public_openai_gateway_policy",
]
