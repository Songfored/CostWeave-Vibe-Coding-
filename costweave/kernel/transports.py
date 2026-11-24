"""Lock-free streaming HTTP/SSE gateway transport and exact micro-USD accounting."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import ssl
import time
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Callable, Protocol
from urllib.parse import urlparse

from ..adapters.http import AdapterTransportError, UrllibHTTPTransport, _PinnedHTTPSConnection
from ..security_boundary import (
    SecretBroker,
    SecretLease,
    SecretLeaseError,
    SecretReference,
    validate_endpoint,
)
from .contracts import canonical_json
from .ports import AdmissionDenied, CancellationToken, KnownFailure


MICRO_USD_PER_DOLLAR = 1_000_000
_MAX_STREAM_BYTES = 8_000_000
_FENCE_RE_START = ("```json", "```")


class TransportError(KnownFailure):
    """Raised on deterministic transport failure or secret exposure."""


@dataclass(frozen=True, slots=True)
class ModelEndpointConfig:
    name: str
    provider_family: str
    endpoint: str
    model_id: str
    deployment: str = "remote"
    input_price_per_mtok_usd: float = 0.0
    cached_input_price_per_mtok_usd: float | None = None
    output_price_per_mtok_usd: float = 0.0
    reasoning_price_per_mtok_usd: float | None = None
    secret_env_var: str | None = None
    timeout_seconds: float = 30.0
    billing_header: str | None = None
    max_output_tokens: int = 4096

    def __post_init__(self) -> None:
        if self.deployment not in ("remote", "local"):
            raise ValueError("deployment must be remote or local")
        validate_endpoint(self.endpoint, deployment=self.deployment)
        for field_name, val in (
            ("input_price_per_mtok_usd", self.input_price_per_mtok_usd),
            ("output_price_per_mtok_usd", self.output_price_per_mtok_usd),
            ("timeout_seconds", self.timeout_seconds),
        ):
            if type(val) not in (int, float) or not math.isfinite(val) or val < 0:
                raise ValueError(f"{field_name} must be finite and >= 0")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        for opt_name, opt_val in (
            ("cached_input_price_per_mtok_usd", self.cached_input_price_per_mtok_usd),
            ("reasoning_price_per_mtok_usd", self.reasoning_price_per_mtok_usd),
        ):
            if opt_val is not None and (type(opt_val) not in (int, float) or not math.isfinite(opt_val) or opt_val < 0):
                raise ValueError(f"{opt_name} must be finite and >= 0")
        if self.secret_env_var is not None:
            SecretReference(self.secret_env_var)
        if type(self.max_output_tokens) is not int or not 1 <= self.max_output_tokens <= 131_072:
            raise ValueError("max_output_tokens must be in [1, 131072]")


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0


@dataclass(frozen=True, slots=True)
class TransportReceipt:
    content: str
    usage: TokenUsage | None
    actual_units: int | None
    billing_source: str
    latency_ms: int
    aborted: bool
    chunks_received: int
    receipt_id: str | None = None


@dataclass(slots=True)
class StreamResponse:
    status_code: int
    headers: Mapping[str, str]
    chunks: Iterable[bytes]
    close_fn: Callable[[], None] | None = None

    def close(self) -> None:
        if self.close_fn is not None:
            self.close_fn()


class StreamChunkTransport(Protocol):
    def stream_post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        cancel_token: CancellationToken | None = None,
    ) -> StreamResponse: ...


class SafeHTTPStreamTransport:
    """Standard-library HTTP/HTTPS streaming transport with subprocess DNS pinning."""

    def __init__(self, *, deployment: str = "remote", timeout_seconds: float = 30.0) -> None:
        self.deployment = deployment
        self.timeout_seconds = timeout_seconds

    def stream_post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        cancel_token: CancellationToken | None = None,
    ) -> StreamResponse:
        validated = validate_endpoint(url, deployment=self.deployment)
        parsed = urlparse(validated)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        path = parsed.path or "/"
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))

        if self.deployment == "remote":
            try:
                approved = UrllibHTTPTransport()._resolve_remote(
                    validated, deadline_monotonic=deadline
                )
            except AdapterTransportError as exc:
                raise TransportError(str(exc)) from exc
            conn: http.client.HTTPConnection = _PinnedHTTPSConnection(
                parsed.hostname or "",
                port,
                approved,
                timeout=max(0.1, deadline - time.monotonic()),
                context=ssl.create_default_context(),
                deadline_monotonic=deadline,
            )
        else:
            conn = http.client.HTTPConnection(
                parsed.hostname or "127.0.0.1",
                port=port,
                timeout=max(0.1, deadline - time.monotonic()),
            )

        conn.request("POST", path, body=body, headers=dict(headers))
        resp = conn.getresponse()
        resp_headers = {k.lower(): v for k, v in resp.getheaders()}

        def iter_lines() -> Iterator[bytes]:
            total = 0
            try:
                while True:
                    if cancel_token is not None and cancel_token.cancelled:
                        break
                    if time.monotonic() > deadline:
                        raise TransportError("stream_deadline_exceeded")
                    line = resp.readline(65_536)
                    if not line:
                        break
                    total += len(line)
                    if total > _MAX_STREAM_BYTES:
                        raise TransportError("stream_response_too_large")
                    yield line
            finally:
                conn.close()

        return StreamResponse(
            status_code=resp.status,
            headers=resp_headers,
            chunks=iter_lines(),
            close_fn=conn.close,
        )


def compute_micro_usd(
    config: ModelEndpointConfig,
    usage: TokenUsage | None,
    headers: Mapping[str, str],
    *,
    aborted: bool = False,
    partial_content: str = "",
    prompt_text: str = "",
) -> tuple[int | None, str]:
    """Compute exact integer micro-USD (1 unit = $1e-6) with conservative fallback."""
    lowered = {k.lower(): v for k, v in headers.items()}
    if config.billing_header and config.billing_header.lower() in lowered:
        raw_bill = str(lowered[config.billing_header.lower()]).strip()
        try:
            dec = Decimal(raw_bill)
            if dec.is_finite() and dec >= 0:
                units = int((dec * MICRO_USD_PER_DOLLAR).to_integral_value(rounding=ROUND_CEILING))
                return units, "provider_billed"
        except InvalidOperation:
            pass

    if usage is not None:
        cached = max(0, min(usage.prompt_tokens, usage.cached_tokens))
        uncached_in = max(0, usage.prompt_tokens - cached)
        cached_rate = Decimal(str(
            config.cached_input_price_per_mtok_usd
            if config.cached_input_price_per_mtok_usd is not None
            else config.input_price_per_mtok_usd
        ))
        in_rate = Decimal(str(config.input_price_per_mtok_usd))
        out_rate = Decimal(str(config.output_price_per_mtok_usd))
        r_rate = Decimal(str(
            config.reasoning_price_per_mtok_usd
            if config.reasoning_price_per_mtok_usd is not None
            else config.output_price_per_mtok_usd
        ))
        reasoning_toks = max(0, usage.reasoning_tokens)
        normal_out = max(0, usage.completion_tokens - reasoning_toks)
        micro = (
            Decimal(uncached_in) * in_rate
            + Decimal(cached) * cached_rate
            + Decimal(normal_out) * out_rate
            + Decimal(reasoning_toks) * r_rate
        )
        return int(micro.to_integral_value(rounding=ROUND_CEILING)), "usage_exact"

    if config.deployment == "local" and config.input_price_per_mtok_usd == 0 and config.output_price_per_mtok_usd == 0:
        return 0, "local_zero"

    if aborted:
        est_in = max(1, len(prompt_text.encode("utf-8")) // 4)
        est_out = max(0, len(partial_content.encode("utf-8")) // 3)
        micro = (
            Decimal(est_in) * Decimal(str(config.input_price_per_mtok_usd))
            + Decimal(est_out) * Decimal(str(config.output_price_per_mtok_usd))
        )
        return int(micro.to_integral_value(rounding=ROUND_CEILING)), "aborted_partial"

    return None, "quote_fallback"


def _parse_usage(raw_usage: Mapping[str, Any] | None) -> TokenUsage | None:
    if not isinstance(raw_usage, Mapping):
        return None
    prompt_toks = int(raw_usage.get("prompt_tokens", raw_usage.get("input_tokens", 0)) or 0)
    comp_toks = int(raw_usage.get("completion_tokens", raw_usage.get("output_tokens", 0)) or 0)
    prompt_details = raw_usage.get("prompt_tokens_details") or {}
    comp_details = raw_usage.get("completion_tokens_details") or {}
    cached = int(raw_usage.get("cached_tokens", prompt_details.get("cached_tokens", 0)) or 0)
    reasoning = int(raw_usage.get("reasoning_tokens", comp_details.get("reasoning_tokens", 0)) or 0)
    return TokenUsage(
        prompt_tokens=max(0, prompt_toks),
        cached_tokens=max(0, cached),
        completion_tokens=max(0, comp_toks),
        reasoning_tokens=max(0, reasoning),
    )


class OpenAICompatibleTransport:
    """Lock-free OpenAI-compatible HTTP + SSE transport with cancellation and secret redaction."""

    def __init__(
        self,
        config: ModelEndpointConfig,
        *,
        transport: StreamChunkTransport | None = None,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.config = config
        self.transport = transport or SafeHTTPStreamTransport(
            deployment=config.deployment, timeout_seconds=config.timeout_seconds,
        )
        self.secret_broker = secret_broker or SecretBroker()

    def invoke(
        self,
        *,
        prompt: str,
        inputs_payload: Mapping[str, Any],
        idempotency_key: str,
        stream: bool = True,
        cancel_token: CancellationToken | None = None,
    ) -> TransportReceipt:
        t0 = time.perf_counter()
        lease: SecretLease | None = None
        if self.config.secret_env_var is not None:
            try:
                lease = self.secret_broker.lease(
                    adapter_id=f"kernel-stream:{self.config.name}",
                    reference=SecretReference(self.config.secret_env_var),
                )
            except SecretLeaseError as exc:
                raise AdmissionDenied("secret_unavailable") from exc
        try:
            headers = {
                "Content-Type": "application/json",
                "Accept": "text/event-stream, application/json",
                "Idempotency-Key": idempotency_key,
            }
            if lease is not None:
                try:
                    headers["Authorization"] = f"Bearer {lease.bearer_value()}"
                except SecretLeaseError as exc:
                    raise AdmissionDenied("secret_lease_expired") from exc
            user_content = canonical_json({"instruction": prompt, "inputs": dict(inputs_payload)})
            req_body: dict[str, Any] = {
                "model": self.config.model_id,
                "messages": [
                    {"role": "system", "content": "Return valid strict JSON matching the requested artifact schema."},
                    {"role": "user", "content": user_content},
                ],
                "max_tokens": self.config.max_output_tokens,
                "stream": bool(stream),
            }
            if stream:
                req_body["stream_options"] = {"include_usage": True}

            resp = self.transport.stream_post(
                url=self.config.endpoint,
                headers=headers,
                body=canonical_json(req_body).encode("utf-8"),
                timeout_seconds=self.config.timeout_seconds,
                cancel_token=cancel_token,
            )
            try:
                if not 200 <= resp.status_code < 300:
                    raise TransportError(f"provider_http_{resp.status_code}")

                content_type = str({k.lower(): v for k, v in resp.headers.items()}.get("content-type", "")).lower()
                is_sse = stream and ("text/event-stream" in content_type or not content_type)
                parts: list[str] = []
                usage: TokenUsage | None = None
                aborted = False
                chunks_seen = 0
                raw_chunks: list[bytes] = []

                if is_sse:
                    for raw_line in resp.chunks:
                        if cancel_token is not None and cancel_token.cancelled:
                            aborted = True
                            resp.close()
                            break
                        if lease is not None:
                            lease.reject_if_exposed(raw_line)
                        chunks_seen += 1
                        line = raw_line.decode("utf-8", errors="strict").strip()
                        if not line or line.startswith(":"):
                            continue
                        if not line.startswith("data:"):
                            # Fallback: server returned plain JSON despite stream=True
                            raw_chunks.append(raw_line)
                            continue
                        data_str = line[5:].strip()
                        if data_str == "[DONE]":
                            break
                        event = json.loads(data_str)
                        if "usage" in event and event["usage"] is not None:
                            usage = _parse_usage(event["usage"])
                        choices = event.get("choices") or ()
                        if choices:
                            delta = choices[0].get("delta") or choices[0].get("message") or {}
                            if isinstance(delta.get("content"), str):
                                parts.append(delta["content"])
                else:
                    for chunk in resp.chunks:
                        if cancel_token is not None and cancel_token.cancelled:
                            aborted = True
                            resp.close()
                            break
                        if lease is not None:
                            lease.reject_if_exposed(chunk)
                        chunks_seen += 1
                        raw_chunks.append(chunk)

                if raw_chunks and not parts and not aborted:
                    body_obj = json.loads(b"".join(raw_chunks).decode("utf-8"))
                    if lease is not None:
                        lease.reject_if_exposed(body_obj)
                    usage = _parse_usage(body_obj.get("usage"))
                    choices = body_obj.get("choices") or ()
                    if choices:
                        msg = choices[0].get("message") or {}
                        parts.append(str(msg.get("content") or ""))

                text = "".join(parts)
                if lease is not None:
                    lease.reject_if_exposed(text)
                units, source = compute_micro_usd(
                    self.config,
                    usage,
                    resp.headers,
                    aborted=aborted,
                    partial_content=text,
                    prompt_text=user_content,
                )
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                rid = "tr_" + hashlib.sha256(f"{idempotency_key}:{chunks_seen}:{elapsed_ms}".encode()).hexdigest()[:24]
                return TransportReceipt(
                    content=text,
                    usage=usage,
                    actual_units=units,
                    billing_source=source,
                    latency_ms=elapsed_ms,
                    aborted=aborted,
                    chunks_received=chunks_seen,
                    receipt_id=rid,
                )
            except SecretLeaseError as exc:
                raise TransportError("secret_exposed_in_response") from exc
            finally:
                resp.close()
        finally:
            if lease is not None:
                lease.close()
