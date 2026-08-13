"""LiteLLM Gateway interoperability for v0.6.6 V066-WP03.

WP03 treats LiteLLM as an injected external gateway.  CostWeave does not
import the LiteLLM package, manage provider credentials, or open a socket in
this module.  A caller must explicitly enable the runtime policy and inject a
small completion transport.  The transport response is then mapped into the
provider-neutral v0.6.6 gateway contract.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from .gateway_backend import (
    GATEWAY_SCHEMA_VERSION,
    GatewayBackend,
    GatewayBackendContractError,
    GatewayBackendDisabledError,
    GatewayBillingV1,
    GatewayCapabilitiesV1,
    GatewayCancellationV1,
    GatewayErrorV1,
    GatewayMessageV1,
    GatewayRequestV1,
    GatewayResponseV1,
    GatewayStreamEventV1,
    GatewayToolV1,
    GatewayUsageV1,
)
from .gateway_openai import _error_category, _response_format_output


LITELLM_GATEWAY_REVISION = "litellm-gateway-v1"
LITELLM_GATEWAY_POLICY_REVISION = "litellm-external-gateway-v1"
LITELLM_GATEWAY_DEPLOYMENT = "external_gateway"
LITELLM_BILLING_MODES = ("free", "paid", "unknown")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,191}$")
_MAX_PAYLOAD_BYTES = 2_000_000


class LiteLLMGatewayError(GatewayBackendContractError):
    """Raised when the LiteLLM boundary or response is invalid."""


class LiteLLMTransportError(LiteLLMGatewayError):
    """Raised by an injected transport when the gateway cannot be reached."""


def _model_name(value: Any, name: str) -> str:
    if not isinstance(value, str) or _MODEL_RE.fullmatch(value) is None:
        raise LiteLLMGatewayError(f"{name} is invalid")
    return value


def _non_negative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise LiteLLMGatewayError(f"{name} must be a non-negative int")
    return value


def _provider_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 192:
        return None
    return value.strip()


def _messages(request: GatewayRequestV1) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in request.messages:
        value: dict[str, Any] = {"role": item.role, "content": item.content}
        if item.name is not None:
            value["name"] = item.name
        result.append(value)
    return result


def _tools(tools: tuple[GatewayToolV1, ...]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": item.name,
                "description": item.description,
                "parameters": dict(item.input_schema),
            },
        }
        for item in tools
    ]


def _usage(value: Any) -> GatewayUsageV1 | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise LiteLLMGatewayError("LiteLLM usage is invalid")
    prompt = value.get("prompt_tokens", value.get("input_tokens", 0))
    completion = value.get("completion_tokens", value.get("output_tokens", 0))
    details = value.get("prompt_tokens_details") or value.get("input_tokens_details") or {}
    completion_details = value.get("completion_tokens_details") or value.get("output_tokens_details") or {}
    if not isinstance(details, Mapping) or not isinstance(completion_details, Mapping):
        raise LiteLLMGatewayError("LiteLLM usage details are invalid")
    cached = details.get("cached_tokens", value.get("cached_input_tokens", 0))
    reasoning = completion_details.get("reasoning_tokens", value.get("reasoning_tokens", 0))
    return GatewayUsageV1(
        input_tokens=_non_negative_int(prompt, "input_tokens"),
        output_tokens=_non_negative_int(completion, "output_tokens"),
        cached_input_tokens=_non_negative_int(cached, "cached_input_tokens"),
        reasoning_tokens=_non_negative_int(reasoning, "reasoning_tokens"),
    )


def _cost(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise LiteLLMGatewayError("LiteLLM cost is invalid")
    if value < 0 or value != value or value in {float("inf"), float("-inf")}:
        raise LiteLLMGatewayError("LiteLLM cost is invalid")
    return float(value)


def _billing(payload: Mapping[str, Any], headers: Mapping[str, str]) -> GatewayBillingV1:
    raw_header = None
    for key, value in headers.items():
        if str(key).lower() in {"x-litellm-cost", "x-cost-usd", "x-billed-usd"}:
            raw_header = value
            break
    cost = None
    if raw_header is not None:
        try:
            cost = _cost(float(raw_header))
        except (TypeError, ValueError) as exc:
            raise LiteLLMGatewayError("LiteLLM billing header is invalid") from exc
    if cost is None:
        for key in ("response_cost", "cost"):
            if key in payload:
                cost = _cost(payload.get(key))
                break
    if cost is None:
        hidden = payload.get("_hidden_params")
        if isinstance(hidden, Mapping):
            cost = _cost(hidden.get("response_cost"))
    if cost is None:
        return GatewayBillingV1()
    return GatewayBillingV1(source="provider", total_cost_usd=cost, reconciled=True)


@dataclass(frozen=True, slots=True)
class LiteLLMGatewayConfig:
    """Configuration without URLs, credentials or provider SDK objects."""

    backend_id: str
    model_aliases: Mapping[str, str] | None = None
    model_billing_modes: Mapping[str, str] | None = None
    schema_version: str = GATEWAY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not isinstance(self.backend_id, str) or _MODEL_RE.fullmatch(self.backend_id) is None:
            raise LiteLLMGatewayError("backend_id is invalid")
        aliases = {} if self.model_aliases is None else dict(self.model_aliases)
        if len(aliases) > 512:
            raise LiteLLMGatewayError("model_aliases contains too many entries")
        for key, value in aliases.items():
            _model_name(key, "model alias")
            _model_name(value, "LiteLLM model name")
        object.__setattr__(self, "model_aliases", aliases)
        billing_modes = (
            {} if self.model_billing_modes is None else dict(self.model_billing_modes)
        )
        if len(billing_modes) > 512:
            raise LiteLLMGatewayError("model_billing_modes contains too many entries")
        for key, value in billing_modes.items():
            _model_name(key, "billing model name")
            if value not in LITELLM_BILLING_MODES:
                raise LiteLLMGatewayError("model billing mode is invalid")
        object.__setattr__(self, "model_billing_modes", billing_modes)
        if self.schema_version != GATEWAY_SCHEMA_VERSION:
            raise LiteLLMGatewayError(f"schema_version must be {GATEWAY_SCHEMA_VERSION}")

    def resolve_model(self, requested: str) -> str:
        _model_name(requested, "requested model")
        return self.model_aliases.get(requested, requested)

    def billing_mode(self, requested: str) -> str:
        resolved = self.resolve_model(requested)
        return self.model_billing_modes.get(
            requested,
            self.model_billing_modes.get(resolved, "unknown"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend_id": self.backend_id,
            "model_aliases": dict(self.model_aliases),
            "model_billing_modes": dict(self.model_billing_modes),
            "deployment": LITELLM_GATEWAY_DEPLOYMENT,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class LiteLLMRuntimePolicy:
    """Explicit admission switch for an injected LiteLLM gateway."""

    gateway_enabled: bool = False
    paid_calls_enabled: bool = False
    policy_revision: str = LITELLM_GATEWAY_POLICY_REVISION

    def __post_init__(self) -> None:
        if not isinstance(self.gateway_enabled, bool) or not isinstance(self.paid_calls_enabled, bool):
            raise LiteLLMGatewayError("LiteLLM policy flags must be bool")
        if self.policy_revision != LITELLM_GATEWAY_POLICY_REVISION:
            raise LiteLLMGatewayError(f"policy_revision must be {LITELLM_GATEWAY_POLICY_REVISION}")

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": GATEWAY_SCHEMA_VERSION,
            "policy_revision": self.policy_revision,
            "gateway_enabled": self.gateway_enabled,
            "paid_calls_enabled": self.paid_calls_enabled,
            "credentials_in_core": False,
            "provider_sdk_imported": False,
        }


@dataclass(frozen=True, slots=True)
class LiteLLMResponse:
    status_code: int = 200
    payload: Mapping[str, Any] | list[Any] | None = None
    headers: Mapping[str, str] | None = None
    elapsed_ms: int = 0

    def __post_init__(self) -> None:
        if isinstance(self.status_code, bool) or not isinstance(self.status_code, int) or not 100 <= self.status_code <= 599:
            raise LiteLLMTransportError("status_code is invalid")
        if self.payload is not None and not isinstance(self.payload, (Mapping, list)):
            raise LiteLLMTransportError("payload must be an object or list")
        if self.headers is not None and not isinstance(self.headers, Mapping):
            raise LiteLLMTransportError("headers must be an object")
        if isinstance(self.elapsed_ms, bool) or not isinstance(self.elapsed_ms, int) or self.elapsed_ms < 0:
            raise LiteLLMTransportError("elapsed_ms is invalid")


class LiteLLMCallTransport(Protocol):
    """Small injected surface implemented by a LiteLLM proxy or test fake."""

    def complete(self, *, payload: Mapping[str, Any]) -> LiteLLMResponse:
        ...


class FakeLiteLLMServer:
    """Deterministic in-process LiteLLM facade; it never opens a socket."""

    evidence_level = "E0-fake-litellm"

    def __init__(
        self,
        *,
        content: str = "fake LiteLLM response",
        model: str | None = None,
        provider_request_id: str = "litellm-request-001",
        input_tokens: int = 11,
        output_tokens: int = 5,
        cached_input_tokens: int = 2,
        response_cost: float | None = None,
        status_code: int = 200,
    ) -> None:
        self.content = content
        self.model = model
        self.provider_request_id = provider_request_id
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cached_input_tokens = cached_input_tokens
        self.response_cost = response_cost
        self.status_code = status_code
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, payload: Mapping[str, Any]) -> LiteLLMResponse:
        self.calls.append(json.loads(json.dumps(dict(payload), ensure_ascii=False)))
        model = self.model or str(payload.get("model", "unknown"))
        body: dict[str, Any] = {
            "id": self.provider_request_id,
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": self.content},
                "finish_reason": "stop",
            }],
        }
        if self.status_code < 300:
            body["usage"] = {
                "prompt_tokens": self.input_tokens,
                "completion_tokens": self.output_tokens,
                "prompt_tokens_details": {"cached_tokens": self.cached_input_tokens},
            }
            if self.response_cost is not None:
                body["_hidden_params"] = {"response_cost": self.response_cost}
        else:
            body = {"error": {"code": "fake_litellm_error", "message": "fixture error"}}
        return LiteLLMResponse(
            status_code=self.status_code,
            payload=body,
            headers={"x-litellm-call-id": self.provider_request_id},
            elapsed_ms=1,
        )


class LiteLLMGatewayBackend:
    """Provider-neutral backend backed by an explicitly injected LiteLLM call."""

    def __init__(
        self,
        config: LiteLLMGatewayConfig,
        *,
        policy: LiteLLMRuntimePolicy | None = None,
        transport: LiteLLMCallTransport | None = None,
    ) -> None:
        self.config = config
        self.policy = policy or LiteLLMRuntimePolicy()
        self.transport = transport
        self._cancelled: set[str] = set()
        self._capabilities = GatewayCapabilitiesV1(
            backend_id=config.backend_id,
            protocol="litellm",
            deployment=LITELLM_GATEWAY_DEPLOYMENT,
            modalities=("text",),
            supports_streaming=False,
            supports_cancellation=False,
            supports_structured_output=True,
            supports_tools=True,
            reports_usage=True,
            reports_billing=True,
        )

    @property
    def capabilities(self) -> GatewayCapabilitiesV1:
        return self._capabilities

    def _admit(self, request: GatewayRequestV1) -> LiteLLMCallTransport:
        if not self.policy.gateway_enabled:
            raise GatewayBackendDisabledError("LiteLLM gateway is disabled by policy")
        billing_mode = self.config.billing_mode(request.model)
        if billing_mode != "free" and not self.policy.paid_calls_enabled:
            raise GatewayBackendDisabledError(
                "LiteLLM paid or unknown-cost model is disabled by policy"
            )
        if self.transport is None:
            raise GatewayBackendDisabledError("LiteLLM transport must be explicitly injected")
        return self.transport

    def _payload(self, request: GatewayRequestV1) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.resolve_model(request.model),
            "messages": _messages(request),
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        if request.tools:
            payload["tools"] = _tools(request.tools)
        if request.response_format is not None:
            payload["response_format"] = dict(request.response_format)
        if request.temperature is not None:
            payload["temperature"] = request.temperature
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw) > _MAX_PAYLOAD_BYTES:
            raise LiteLLMGatewayError("LiteLLM request exceeds safe size")
        return payload

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        for key, value in headers.items():
            if str(key).lower() == name.lower():
                return str(value)
        return None

    def _failure(self, request: GatewayRequestV1, *, category: str, retryable: bool, message: str, provider_request_id: str | None = None) -> GatewayResponseV1:
        return GatewayResponseV1(
            request_id=request.request_id,
            status="failed",
            provider_request_id=_provider_id(provider_request_id),
            error=GatewayErrorV1(
                category=category,
                message=message,
                retryable=retryable,
                provider_request_id=_provider_id(provider_request_id),
            ),
        )

    def _completed(self, request: GatewayRequestV1, result: LiteLLMResponse) -> GatewayResponseV1:
        headers = result.headers or {}
        provider_id = self._header(headers, "x-litellm-call-id") or self._header(headers, "x-request-id")
        if isinstance(result.payload, Mapping):
            provider_id = _provider_id(result.payload.get("id")) or _provider_id(provider_id)
        if not 200 <= result.status_code < 300:
            category, retryable = _error_category(result.status_code)
            return self._failure(
                request,
                category=category,
                retryable=retryable,
                message=f"LiteLLM gateway returned HTTP {result.status_code}",
                provider_request_id=provider_id,
            )
        if not isinstance(result.payload, Mapping):
            return self._failure(request, category="protocol", retryable=False, message="LiteLLM response must be an object", provider_request_id=provider_id)
        try:
            choices = result.payload.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], Mapping):
                raise LiteLLMGatewayError("choices are missing")
            message = choices[0].get("message")
            if not isinstance(message, Mapping):
                raise LiteLLMGatewayError("message is missing")
            content = message.get("content", "")
            if content is None:
                content = ""
            if not isinstance(content, str):
                raise LiteLLMGatewayError("text content is invalid")
            tool_calls = message.get("tool_calls") or ()
            if not isinstance(tool_calls, (list, tuple)) or any(not isinstance(item, Mapping) for item in tool_calls):
                raise LiteLLMGatewayError("tool calls are invalid")
            return GatewayResponseV1(
                request_id=request.request_id,
                status="completed",
                content=content,
                structured_output=_response_format_output(request, content),
                tool_calls=tuple(dict(item) for item in tool_calls),
                usage=_usage(result.payload.get("usage")),
                billing=_billing(result.payload, headers),
                provider_request_id=provider_id,
            )
        except (LiteLLMGatewayError, GatewayBackendContractError):
            return self._failure(request, category="protocol", retryable=False, message="LiteLLM response violated the gateway contract", provider_request_id=provider_id)

    def submit(self, request: GatewayRequestV1) -> GatewayResponseV1:
        transport = self._admit(request)
        if request.stream:
            raise LiteLLMGatewayError("streaming request must use stream()")
        if request.request_id in self._cancelled:
            return self._failure(request, category="cancelled", retryable=False, message="request was cancelled before LiteLLM call")
        try:
            return self._completed(request, transport.complete(payload=self._payload(request)))
        except GatewayBackendDisabledError:
            raise
        except LiteLLMTransportError:
            return self._failure(request, category="network", retryable=True, message="LiteLLM transport failed")
        except (LiteLLMGatewayError, GatewayBackendContractError):
            return self._failure(
                request,
                category="protocol",
                retryable=False,
                message="LiteLLM request or response violated the gateway contract",
            )
        except Exception:
            return self._failure(
                request,
                category="unknown",
                retryable=False,
                message="LiteLLM transport failed without a retry-safe classification",
            )

    def stream(self, request: GatewayRequestV1):
        self._admit(request)
        raise LiteLLMGatewayError("LiteLLM streaming is deferred to a later work package")

    def cancel(self, request_id: str, *, reason: str) -> GatewayCancellationV1:
        if not isinstance(request_id, str) or not request_id.strip() or not isinstance(reason, str) or not reason.strip():
            raise LiteLLMGatewayError("request_id and reason are required")
        self._cancelled.add(request_id)
        return GatewayCancellationV1(request_id=request_id, accepted=False, reason="LiteLLM cancellation is not supported by WP03")


def public_litellm_gateway_policy() -> dict[str, Any]:
    return LiteLLMRuntimePolicy().public()


__all__ = [
    "FakeLiteLLMServer",
    "LiteLLMCallTransport",
    "LiteLLMGatewayBackend",
    "LiteLLMGatewayConfig",
    "LiteLLMGatewayError",
    "LiteLLMResponse",
    "LiteLLMRuntimePolicy",
    "LiteLLMTransportError",
    "LITELLM_GATEWAY_DEPLOYMENT",
    "LITELLM_BILLING_MODES",
    "LITELLM_GATEWAY_POLICY_REVISION",
    "LITELLM_GATEWAY_REVISION",
    "public_litellm_gateway_policy",
]
