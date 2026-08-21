"""Default-off, capability-scoped HTTP Adapter plugins for v0.5.8.2."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import socket
import ssl
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

from ..evaluation_protocol import canonical_json
from ..experimental_calls import (
    REAL_UNVALIDATED_EVIDENCE,
    ExperimentalCallError,
    ExperimentalCallStore,
    ExternalAttempt,
    ExternalBudgetExceededError,
    ExternalCallInProgressError,
    ExternalCallsDisabledError,
)
from ..planning_contracts import ResultEnvelope, WorkOrder
from ..external_call_authority import (
    ExternalCallAuthorityError,
    ExternalCallAuthorityV1,
    ExternalTransportCapabilityV1,
)
from ..security_boundary import (
    DEFAULT_DATA_RESIDENCY,
    DEFAULT_PROJECT_ID,
    MAX_RESPONSE_BYTES,
    AdapterPluginManifest,
    SecretBroker,
    SecretLease,
    SecretLeaseError,
    SecretReference,
    normalize_data_residency,
    normalize_project_id,
    redact_text,
    validate_endpoint,
    validate_remote_resolution,
)


HTTP_ADAPTER_SCHEMA_VERSION = "v0.5.8.2"
OPENAI_COMPATIBLE_ADAPTER_REVISION = "openai-compatible-http-v1"
LOCAL_HTTP_ADAPTER_REVISION = "local-http-v1"

SECRET_LIKE = re.compile(
    r"(?i)(?:bearer\s+|api[_-]?key[=: ]+|sk-)[A-Za-z0-9._~+/-]{6,}"
)


class AdapterTransportError(ExperimentalCallError):
    """Categorical transport failure with no response content."""


@dataclass(frozen=True, slots=True)
class AdapterRuntimePolicy:
    experimental_calls_enabled: bool = False
    paid_calls_enabled: bool = False

    @classmethod
    def from_environment(cls) -> "AdapterRuntimePolicy":
        return cls(
            experimental_calls_enabled=(
                os.environ.get("COSTWEAVE_EXPERIMENTAL_ADAPTERS", "0")
                == "1"
            ),
            paid_calls_enabled=(
                os.environ.get("COSTWEAVE_PAID_CALLS", "0") == "1"
            ),
        )

    def public(self) -> dict[str, bool]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ExperimentalAdapterConfig:
    adapter_id: str
    provider: str
    endpoint: str
    model_id: str
    deployment: str
    input_price_per_mtok_usd: float = 0.0
    output_price_per_mtok_usd: float = 0.0
    timeout_seconds: float = 30.0
    max_retries: int = 0
    secret_env: str | None = None
    reasoning_effort: str = "medium"
    billing_header: str | None = None
    adapter_revision: str = OPENAI_COMPATIBLE_ADAPTER_REVISION

    def __post_init__(self) -> None:
        if not all(
            value.strip()
            for value in (
                self.adapter_id,
                self.provider,
                self.endpoint,
                self.model_id,
            )
        ):
            raise ExperimentalCallError("adapter identity is required")
        if self.deployment not in {"local", "remote"}:
            raise ExperimentalCallError("adapter deployment is invalid")
        if min(
            self.input_price_per_mtok_usd,
            self.output_price_per_mtok_usd,
            self.timeout_seconds,
        ) < 0:
            raise ExperimentalCallError("adapter pricing or timeout is invalid")
        if self.timeout_seconds <= 0 or not 0 <= self.max_retries <= 3:
            raise ExperimentalCallError("bounded adapter retry policy is required")
        try:
            validate_endpoint(self.endpoint, deployment=self.deployment)
            if self.secret_env:
                SecretReference(self.secret_env)
        except ValueError as error:
            raise ExperimentalCallError(str(error)) from error

    @property
    def paid(self) -> bool:
        return self.deployment == "remote" or any(
            value > 0
            for value in (
                self.input_price_per_mtok_usd,
                self.output_price_per_mtok_usd,
            )
        )

    @property
    def deployment_id(self) -> str:
        digest = hashlib.sha256(
            canonical_json(
                {
                    "provider": self.provider,
                    "endpoint": self.endpoint,
                    "model_id": self.model_id,
                    "adapter_revision": self.adapter_revision,
                    "reasoning_effort": self.reasoning_effort,
                }
            ).encode("utf-8")
        ).hexdigest()[:24]
        return f"deployment_{digest}"

    def public(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "provider": self.provider,
            "model_id": self.model_id,
            "deployment": self.deployment,
            "deployment_id": self.deployment_id,
            "adapter_revision": self.adapter_revision,
            "reasoning_effort": self.reasoning_effort,
            "paid": self.paid,
            "secret_required": bool(self.secret_env),
            "text_only": True,
            "max_retries": self.max_retries,
        }


@dataclass(frozen=True, slots=True)
class ExternalExecutionRequest:
    experiment_id: str
    run_id: str
    task_id: str
    work_order: WorkOrder
    max_output_tokens: int
    idempotency_key: str
    batch: bool = False
    project_id: str = DEFAULT_PROJECT_ID
    data_residency: str = DEFAULT_DATA_RESIDENCY
    security_approval_id: str | None = None

    def __post_init__(self) -> None:
        if not self.experiment_id.startswith("experiment_"):
            raise ExperimentalCallError("experiment id is invalid")
        if not self.run_id or not self.task_id:
            raise ExperimentalCallError("run and task attribution are required")
        if self.task_id != self.work_order.task_id:
            raise ExperimentalCallError("request and WorkOrder task disagree")
        if not 1 <= self.max_output_tokens <= 65536:
            raise ExperimentalCallError("bounded output token limit is required")
        if self.batch:
            raise ExperimentalCallError("batch execution is disabled in the current release")
        try:
            normalize_project_id(self.project_id)
            normalize_data_residency(self.data_residency)
        except ValueError as error:
            raise ExperimentalCallError(str(error)) from error


@dataclass(frozen=True, slots=True)
class HTTPResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str]
    elapsed_ms: int


class HTTPTransport(Protocol):
    evidence_level: str

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> HTTPResponse:
        ...


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """Connect to a pre-approved IP while preserving TLS hostname checks."""

    def __init__(
        self,
        hostname: str,
        port: int,
        approved_addresses: tuple[str, ...],
        *,
        timeout: float,
        context: ssl.SSLContext,
    ) -> None:
        super().__init__(hostname, port, timeout=timeout, context=context)
        self._approved_addresses = frozenset(approved_addresses)
        self._pinned_context = context

    def connect(self) -> None:
        last_error: OSError | None = None
        for address in sorted(self._approved_addresses):
            raw_socket = None
            try:
                raw_socket = socket.create_connection(
                    (address, self.port), timeout=self.timeout
                )
                wrapped = self._pinned_context.wrap_socket(
                    raw_socket, server_hostname=self.host
                )
                peer = str(wrapped.getpeername()[0]).split("%", 1)[0]
                if peer not in self._approved_addresses:
                    wrapped.close()
                    raise OSError("connected peer is outside the approved DNS set")
                self.sock = wrapped
                return
            except OSError as error:
                last_error = error
                if raw_socket is not None:
                    try:
                        raw_socket.close()
                    except OSError:
                        pass
        raise OSError("no approved remote address could be connected") from last_error


class UrllibHTTPTransport:
    """Minimal synchronous transport; never used without policy admission."""

    evidence_level = REAL_UNVALIDATED_EVIDENCE

    def __init__(
        self,
        *,
        resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
        ssl_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context,
        pinned_connection_class: type[_PinnedHTTPSConnection] = _PinnedHTTPSConnection,
    ) -> None:
        self._resolver = resolver
        self._ssl_context_factory = ssl_context_factory
        self._pinned_connection_class = pinned_connection_class

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def _post_remote_pinned(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> HTTPResponse:
        parsed = urlparse(url)
        addresses = validate_remote_resolution(url, resolver=self._resolver)
        connection = self._pinned_connection_class(
            str(parsed.hostname),
            int(parsed.port or 443),
            addresses,
            timeout=timeout_seconds,
            context=self._ssl_context_factory(),
        )
        started = time.monotonic()
        try:
            connection.request(
                "POST",
                parsed.path or "/",
                body=body,
                headers=dict(headers),
            )
            response = connection.getresponse()
            payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise AdapterTransportError("response_too_large")
            return HTTPResponse(
                status_code=int(response.status),
                body=payload,
                headers=dict(response.getheaders()),
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        except AdapterTransportError:
            raise
        except Exception as error:
            raise AdapterTransportError("transport_error") from error
        finally:
            connection.close()

    def post(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
    ) -> HTTPResponse:
        hostname = (urlparse(url).hostname or "").rstrip(".").lower()
        if hostname in {"127.0.0.1", "localhost", "::1"}:
            validate_endpoint(url, deployment="local")
        else:
            return self._post_remote_pinned(
                url=url,
                headers=headers,
                body=body,
                timeout_seconds=timeout_seconds,
            )
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
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise AdapterTransportError("response_too_large")
                return HTTPResponse(
                    status_code=int(response.status),
                    body=payload,
                    headers=dict(response.headers.items()),
                    elapsed_ms=round(
                        (time.monotonic() - started) * 1000
                    ),
                )
        except urllib.error.HTTPError as error:
            try:
                payload = error.read(MAX_RESPONSE_BYTES + 1)
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise AdapterTransportError("response_too_large")
                return HTTPResponse(
                    status_code=int(error.code),
                    body=payload,
                    headers=dict(error.headers.items()),
                    elapsed_ms=round(
                        (time.monotonic() - started) * 1000
                    ),
                )
            finally:
                error.close()
        except Exception as error:
            raise AdapterTransportError("transport_error") from error


@dataclass(frozen=True, slots=True)
class ParsedModelResponse:
    content: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0

    def __post_init__(self) -> None:
        if min(
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
        ) < 0:
            raise ExperimentalCallError("provider token usage is invalid")


@dataclass(frozen=True, slots=True)
class ExternalCallReceipt:
    external_call_id: str
    result: ResultEnvelope
    telemetry: Mapping[str, Any]
    replayed: bool = False
    schema_version: str = HTTP_ADAPTER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "external_call_id": self.external_call_id,
            "result": self.result.to_dict(),
            "telemetry": dict(self.telemetry),
            "replayed": self.replayed,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, replayed: bool
    ) -> "ExternalCallReceipt":
        result = dict(value["result"])
        envelope = ResultEnvelope(
            work_order_id=str(result["work_order_id"]),
            status=str(result["status"]),
            output=dict(result["output"]),
            evidence=tuple(result.get("evidence", ())),
            acceptance_results=tuple(
                result.get("acceptance_results", ())
            ),
            assumptions_used=tuple(result.get("assumptions_used", ())),
            unresolved_risks=tuple(result.get("unresolved_risks", ())),
            actual_cost=float(result["actual_cost"]),
            actual_latency_ms=int(result["actual_latency_ms"]),
            confidence=float(result["confidence"]),
            escalation_requested=bool(
                result.get("escalation_requested", False)
            ),
            escalation_reason=str(result.get("escalation_reason", "")),
            schema_version=str(result.get("schema_version", "v0.5.1")),
        )
        return cls(
            external_call_id=str(value["external_call_id"]),
            result=envelope,
            telemetry=dict(value["telemetry"]),
            replayed=replayed,
            schema_version=str(
                value.get("schema_version", HTTP_ADAPTER_SCHEMA_VERSION)
            ),
        )


def _safe_error_code(error: BaseException) -> str:
    if isinstance(error, AdapterTransportError):
        return "transport_error"
    if isinstance(error, SecretLeaseError):
        return "secret_exposure_detected"
    if isinstance(error, json.JSONDecodeError):
        return "invalid_json"
    if isinstance(error, ExperimentalCallError):
        raw = SECRET_LIKE.sub("[REDACTED]", str(error)).lower()
        if "usage" in raw or "token" in raw:
            return "invalid_usage"
        return "adapter_contract_error"
    return "adapter_internal_error"


def _provider_idempotency_key(value: str) -> str:
    if re.fullmatch(r"[A-Za-z0-9._:-]{8,192}", value):
        return value
    return "provider_" + hashlib.sha256(value.encode("utf-8")).hexdigest()


class BaseExperimentalHTTPAdapter(ABC):
    PLUGIN_ID = ""

    def __init__(
        self,
        config: ExperimentalAdapterConfig,
        *,
        store: ExperimentalCallStore,
        policy: AdapterRuntimePolicy | None = None,
        transport: HTTPTransport | None = None,
        secret_broker: SecretBroker | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.policy = policy or AdapterRuntimePolicy.from_environment()
        self.transport = transport or UrllibHTTPTransport()
        self.authority = ExternalCallAuthorityV1(
            store,
            secret_broker=secret_broker,
        )
        # Compatibility aliases remain read-only ownership references; all
        # external lifecycle mutations go through ``self.authority``.
        self.secret_broker = self.authority.secret_broker
        self.security_store = self.authority.security_store
        self.approval_store = self.authority.approval_store

    @abstractmethod
    def build_payload(
        self, request: ExternalExecutionRequest
    ) -> Mapping[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def parse_response(
        self, value: Mapping[str, Any]
    ) -> ParsedModelResponse:
        raise NotImplementedError

    def _admit(
        self, request: ExternalExecutionRequest
    ) -> tuple[SecretLease | None, Mapping[str, Any], AdapterPluginManifest]:
        admission = self.authority.admit(
            plugin_id=self.PLUGIN_ID,
            adapter_id=self.config.adapter_id,
            adapter_revision=self.config.adapter_revision,
            deployment=self.config.deployment,
            endpoint=self.config.endpoint,
            secret_env=self.config.secret_env,
            paid=self.config.paid,
            experimental_calls_enabled=self.policy.experimental_calls_enabled,
            paid_calls_enabled=self.policy.paid_calls_enabled,
            project_id=request.project_id,
            data_residency=request.data_residency,
            run_id=request.run_id,
            task_id=request.task_id,
            work_order=request.work_order,
            security_approval_id=request.security_approval_id,
        )
        return (
            admission.secret_lease,
            admission.prompt_security,
            admission.manifest,
        )

    def _headers(
        self,
        secret_lease: SecretLease | None,
        *,
        idempotency_key: str,
    ) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "CostWeave/0.6.7-experimental",
            "Idempotency-Key": idempotency_key,
        }
        if secret_lease is not None:
            headers["Authorization"] = f"Bearer {secret_lease.bearer_value()}"
        return headers

    def _worst_case_cost(
        self, input_token_upper_bound: int, max_output_tokens: int
    ) -> float:
        return round(
            input_token_upper_bound
            / 1_000_000
            * self.config.input_price_per_mtok_usd
            + max_output_tokens
            / 1_000_000
            * self.config.output_price_per_mtok_usd,
            10,
        )

    def _actual_cost(
        self,
        parsed: ParsedModelResponse,
        response: HTTPResponse,
    ) -> tuple[float, str]:
        if self.config.billing_header:
            value = next(
                (
                    header_value
                    for key, header_value in response.headers.items()
                    if key.lower() == self.config.billing_header.lower()
                ),
                None,
            )
            if value is not None:
                try:
                    billed = float(value)
                except ValueError as error:
                    raise ExperimentalCallError(
                        "provider billing header is invalid"
                    ) from error
                if billed < 0:
                    raise ExperimentalCallError(
                        "provider billing header is negative"
                    )
                return round(billed, 10), "provider_billing_header"
        cost = (
            parsed.input_tokens
            / 1_000_000
            * self.config.input_price_per_mtok_usd
            + parsed.output_tokens
            / 1_000_000
            * self.config.output_price_per_mtok_usd
        )
        return round(cost, 10), "usage_pricing_estimate"

    def execute(
        self, request: ExternalExecutionRequest
    ) -> ExternalCallReceipt:
        secret_lease, prompt_assessment, manifest = self._admit(request)
        try:
            return self._execute_admitted(
                request,
                secret_lease=secret_lease,
                prompt_assessment=prompt_assessment,
                manifest=manifest,
            )
        finally:
            if secret_lease is not None:
                secret_lease.close()

    def _replay_completed_side_effect(
        self,
        *,
        reservation: Any,
        side_effect_id: str,
    ) -> ExternalCallReceipt | None:
        state = self.security_store.side_effect(side_effect_id) or {}
        completed = next(
            (
                item
                for item in reversed(state.get("events", []))
                if item.get("status") == "completed"
            ),
            None,
        )
        if completed is None:
            return None
        source_call_id = str(completed.get("detail", {}).get("external_call_id") or "")
        source_call = self.store.call(source_call_id) if source_call_id else None
        source_result = source_call.get("result") if source_call is not None else None
        if not isinstance(source_result, Mapping):
            return None
        prior = ExternalCallReceipt.from_dict(source_result, replayed=True)
        replayed_result = ResultEnvelope(
            work_order_id=prior.result.work_order_id,
            status=prior.result.status,
            output=prior.result.output,
            evidence=prior.result.evidence,
            acceptance_results=prior.result.acceptance_results,
            assumptions_used=prior.result.assumptions_used,
            unresolved_risks=prior.result.unresolved_risks,
            actual_cost=0.0,
            actual_latency_ms=0,
            confidence=prior.result.confidence,
            escalation_requested=prior.result.escalation_requested,
            escalation_reason=prior.result.escalation_reason,
            schema_version=prior.result.schema_version,
        )
        telemetry = {
            **dict(prior.telemetry),
            "external_call_id": reservation.external_call_id,
            "actual_cost_usd": 0.0,
            "attempt_count": 0,
            "global_side_effect_replayed": True,
            "replay_source_external_call_id": source_call_id,
        }
        receipt = ExternalCallReceipt(
            external_call_id=reservation.external_call_id,
            result=replayed_result,
            telemetry=telemetry,
            replayed=True,
        )
        self.store.finalize(
            reservation.external_call_id,
            status="completed",
            result=receipt.to_dict(),
            telemetry=telemetry,
        )
        return receipt

    def _execute_admitted(
        self,
        request: ExternalExecutionRequest,
        *,
        secret_lease: SecretLease | None,
        prompt_assessment: Mapping[str, Any],
        manifest: AdapterPluginManifest,
    ) -> ExternalCallReceipt:
        payload = dict(self.build_payload(request))
        provider_idempotency_key = _provider_idempotency_key(
            request.idempotency_key
        )
        payload_raw = canonical_json(payload).encode("utf-8")
        # A UTF-8 byte count is a conservative upper bound for BPE tokens.
        input_upper = len(payload_raw)
        worst_per_attempt = self._worst_case_cost(
            input_upper, request.max_output_tokens
        )
        if (
            worst_per_attempt * (self.config.max_retries + 1)
            > request.work_order.cost_limit + 1e-10
        ):
            raise ExternalBudgetExceededError(
                "WorkOrder hard cost limit cannot fund worst-case attempts"
            )
        fingerprint = {
            "experiment_id": request.experiment_id,
            "run_id": request.run_id,
            "task_id": request.task_id,
            "project_id": normalize_project_id(request.project_id),
            "data_residency": normalize_data_residency(request.data_residency),
            "work_order_id": request.work_order.work_order_id,
            "work_order_hash": hashlib.sha256(
                canonical_json(request.work_order.to_dict()).encode("utf-8")
            ).hexdigest(),
            "deployment_id": self.config.deployment_id,
            "provider": self.config.provider,
            "model_id": self.config.model_id,
            "adapter_revision": self.config.adapter_revision,
            "max_output_tokens": request.max_output_tokens,
            "payload_hash": hashlib.sha256(payload_raw).hexdigest(),
        }
        payload_hash = hashlib.sha256(payload_raw).hexdigest()
        authority_reservation = self.authority.reserve(
            logical_values={
                "experiment_id": request.experiment_id,
                "run_id": request.run_id,
                "task_id": request.task_id,
                "deployment_id": self.config.deployment_id,
                "provider": self.config.provider,
                "model_id": self.config.model_id,
                "adapter_revision": self.config.adapter_revision,
                "reasoning_effort": self.config.reasoning_effort,
                "idempotency_key": request.idempotency_key,
                "request_fingerprint": fingerprint,
                "worst_case_cost_per_attempt_usd": worst_per_attempt,
                "max_attempts": self.config.max_retries + 1,
            },
            project_id=request.project_id,
            adapter_id=self.config.adapter_id,
            endpoint=self.config.endpoint,
            operation="model_inference",
            provider_idempotency_key=provider_idempotency_key,
            request_hash=payload_hash,
            max_attempts=self.config.max_retries + 1,
        )
        reservation = authority_reservation.logical
        if authority_reservation.mode == "logical_replay":
            if reservation.status == "completed" and reservation.result:
                return ExternalCallReceipt.from_dict(
                    reservation.result, replayed=True
                )
            if reservation.status == "reserved":
                raise ExternalCallInProgressError(
                    "logical external call is already in progress"
                )
            raise ExperimentalCallError(
                f"logical external call already ended: {reservation.error_code}"
            )

        side_effect_id = str(authority_reservation.side_effect_id)
        if authority_reservation.mode == "global_side_effect_replay":
            replay = self._replay_completed_side_effect(
                reservation=reservation,
                side_effect_id=side_effect_id,
            )
            if replay is not None:
                return replay
            self.store.finalize(
                reservation.external_call_id,
                status="failed",
                result=None,
                telemetry={
                    "global_side_effect_replayed": True,
                    "transport_called": False,
                    "actual_cost_usd": 0.0,
                },
                error_code="global_side_effect_replay_blocked",
            )
            raise ExternalCallInProgressError(
                "global external side effect is reserved, failed or ambiguous"
            )
        capability = authority_reservation.capability
        if not isinstance(capability, ExternalTransportCapabilityV1):
            raise ExternalCallAuthorityError(
                "transport authorization did not issue a capability"
            )

        attempts: list[ExternalAttempt] = []
        billing_sources: list[str] = []
        evidence_level = str(
            getattr(
                self.transport,
                "evidence_level",
                "E0-transport-fixture",
            )
        )
        result: ResultEnvelope | None = None
        last_error = "external_call_failed"
        for ordinal in range(1, self.config.max_retries + 2):
            transport_called = False
            try:
                request_headers = self._headers(
                    secret_lease,
                    idempotency_key=provider_idempotency_key,
                )
                self.authority.consume_for_transport(
                    capability,
                    ordinal=ordinal,
                    endpoint=self.config.endpoint,
                    request_body=payload_raw,
                )
                transport_called = True
                response = self.transport.post(
                    url=self.config.endpoint,
                    headers=request_headers,
                    body=payload_raw,
                    timeout_seconds=self.config.timeout_seconds,
                )
                self.authority.guard_response(
                    capability,
                    secret_lease=secret_lease,
                    response_body=response.body,
                )
                queue_ms = 0
                for key, value in response.headers.items():
                    if key.lower() == "x-queue-time-ms":
                        try:
                            queue_ms = max(0, int(float(value)))
                        except ValueError:
                            queue_ms = 0
                response_hash = hashlib.sha256(response.body).hexdigest()
                if response.status_code in {429, 503}:
                    last_error = (
                        "provider_rate_limited"
                        if response.status_code == 429
                        else "provider_unavailable"
                    )
                    attempt = ExternalAttempt(
                        ordinal=ordinal,
                        status="rate_limited",
                        status_code=response.status_code,
                        request_latency_ms=response.elapsed_ms,
                        queue_latency_ms=queue_ms,
                        actual_cost_usd=worst_per_attempt,
                        rate_limited=True,
                        error_code=last_error,
                        response_hash=response_hash,
                    )
                    self.store.record_attempt(
                        reservation.external_call_id, attempt
                    )
                    attempts.append(attempt)
                    if ordinal <= self.config.max_retries:
                        continue
                    break
                if not 200 <= response.status_code < 300:
                    last_error = "provider_http_error"
                    attempt = ExternalAttempt(
                        ordinal=ordinal,
                        status="failed",
                        status_code=response.status_code,
                        request_latency_ms=response.elapsed_ms,
                        queue_latency_ms=queue_ms,
                        actual_cost_usd=worst_per_attempt,
                        error_code=last_error,
                        response_hash=response_hash,
                    )
                    self.store.record_attempt(
                        reservation.external_call_id, attempt
                    )
                    attempts.append(attempt)
                    break
                value = json.loads(response.body.decode("utf-8"))
                if not isinstance(value, Mapping):
                    raise ExperimentalCallError(
                        "provider response must be a JSON object"
                    )
                parsed = self.parse_response(value)
                actual_cost, billing_source = self._actual_cost(
                    parsed, response
                )
                billing_sources.append(billing_source)
                attempt = ExternalAttempt(
                    ordinal=ordinal,
                    status="completed",
                    status_code=response.status_code,
                    input_tokens=parsed.input_tokens,
                    output_tokens=parsed.output_tokens,
                    cached_input_tokens=parsed.cached_input_tokens,
                    actual_cost_usd=actual_cost,
                    request_latency_ms=response.elapsed_ms,
                    queue_latency_ms=queue_ms,
                    response_hash=response_hash,
                )
                self.store.record_attempt(
                    reservation.external_call_id, attempt
                )
                attempts.append(attempt)
                result = ResultEnvelope(
                    work_order_id=request.work_order.work_order_id,
                    status="completed",
                    output={
                        "content": redact_text(parsed.content),
                        "validation_status": "pending_independent_validation",
                    },
                    evidence=(
                        {
                            "kind": "external-call-receipt",
                            "value": reservation.external_call_id,
                            "evidence_level": evidence_level,
                            "provider": self.config.provider,
                            "model_id": self.config.model_id,
                        },
                    ),
                    acceptance_results=tuple(
                        {
                            "criterion": criterion,
                            "passed": False,
                            "status": "not_independently_validated",
                        }
                        for criterion in request.work_order.acceptance_tests
                    ),
                    assumptions_used=request.work_order.assumptions,
                    unresolved_risks=(
                        "external result has not been independently validated",
                    ),
                    actual_cost=sum(
                        item.actual_cost_usd for item in attempts
                    ),
                    actual_latency_ms=sum(
                        item.request_latency_ms for item in attempts
                    ),
                    confidence=0.5,
                )
                break
            except Exception as error:
                last_error = _safe_error_code(error)
                attempt = ExternalAttempt(
                    ordinal=ordinal,
                    status="failed",
                    actual_cost_usd=(worst_per_attempt if transport_called else 0.0),
                    error_code=last_error,
                )
                self.store.record_attempt(
                    reservation.external_call_id, attempt
                )
                attempts.append(attempt)
                if transport_called:
                    self.authority.record_side_effect(
                        capability,
                        status="ambiguous",
                        detail={
                            "transport_called": True,
                            "error_code": last_error,
                            "automatic_replay": False,
                        },
                    )
                break

        telemetry = {
            "schema_version": HTTP_ADAPTER_SCHEMA_VERSION,
            "evidence_level": evidence_level,
            "independently_validated": False,
            "external_call_id": reservation.external_call_id,
            "experiment_id": request.experiment_id,
            "run_id": request.run_id,
            "task_id": request.task_id,
            "project_id": normalize_project_id(request.project_id),
            "data_residency": normalize_data_residency(request.data_residency),
            "deployment_id": self.config.deployment_id,
            "provider": self.config.provider,
            "model_id": self.config.model_id,
            "adapter_revision": self.config.adapter_revision,
            "reasoning_effort": self.config.reasoning_effort,
            "attempt_count": len(attempts),
            "input_tokens": sum(item.input_tokens for item in attempts),
            "output_tokens": sum(item.output_tokens for item in attempts),
            "cached_input_tokens": sum(
                item.cached_input_tokens for item in attempts
            ),
            "actual_cost_usd": round(
                sum(item.actual_cost_usd for item in attempts), 10
            ),
            "request_latency_ms": sum(
                item.request_latency_ms for item in attempts
            ),
            "queue_latency_ms": sum(
                item.queue_latency_ms for item in attempts
            ),
            "rate_limit_count": sum(
                item.rate_limited for item in attempts
            ),
            "cancelled": any(item.cancelled for item in attempts),
            "cache_observed": any(
                item.cached_input_tokens > 0 for item in attempts
            ),
            "batch": False,
            "billing_sources": sorted(set(billing_sources)),
            "commercial_claims_allowed": False,
            "adapter_plugin_id": manifest.plugin_id,
            "prompt_security": dict(prompt_assessment),
            "side_effect_id": side_effect_id,
            "external_call_authority": "v0.6.7",
            "one_shot_transport_capability": True,
            "provider_idempotency_forwarded": True,
            "secret_lease_version": (
                secret_lease.version if secret_lease is not None else None
            ),
            "secret_value_persisted": False,
        }
        if result is None:
            current = self.security_store.side_effect(side_effect_id) or {}
            if not any(
                item.get("status") == "ambiguous"
                for item in current.get("events", [])
            ):
                self.authority.record_side_effect(
                    capability,
                    status="failed",
                    detail={"error_code": last_error},
                )
            self.store.finalize(
                reservation.external_call_id,
                status="failed",
                result=None,
                telemetry=telemetry,
                error_code=last_error,
            )
            self.authority.close_capability(capability)
            raise ExperimentalCallError(
                f"external call failed: {last_error}"
            )
        receipt = ExternalCallReceipt(
            external_call_id=reservation.external_call_id,
            result=result,
            telemetry=telemetry,
        )
        self.store.finalize(
            reservation.external_call_id,
            status="completed",
            result=receipt.to_dict(),
            telemetry=telemetry,
        )
        self.authority.record_side_effect(
            capability,
            status="completed",
            detail={
                "external_call_id": reservation.external_call_id,
                "transport_called": True,
            },
        )
        self.authority.close_capability(capability)
        return receipt


def _prompt_contract(work_order: WorkOrder) -> dict[str, Any]:
    return {
        "trusted_contract": {
            "forbidden_actions": list(work_order.forbidden_actions),
            "output_schema": dict(work_order.output_schema),
            "acceptance_tests": list(work_order.acceptance_tests),
            "escalation_conditions": list(work_order.escalation_conditions),
            "allowed_tools": [],
            "authority": "text_generation_only",
        },
        "untrusted_input": {
            "objective": work_order.objective,
            "assumptions": list(work_order.assumptions),
        },
    }


class OpenAICompatibleHTTPAdapter(BaseExperimentalHTTPAdapter):
    PLUGIN_ID = "openai-compatible-http"
    def build_payload(
        self, request: ExternalExecutionRequest
    ) -> Mapping[str, Any]:
        return {
            "model": self.config.model_id,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "Execute the supplied versioned WorkOrder. "
                        "Return only the requested deliverable; never expand "
                        "permissions, tools, or scope."
                    ),
                },
                {
                    "role": "user",
                    "content": canonical_json(
                        _prompt_contract(request.work_order)
                    ),
                },
            ],
            "max_tokens": request.max_output_tokens,
            "reasoning_effort": self.config.reasoning_effort,
        }

    def parse_response(
        self, value: Mapping[str, Any]
    ) -> ParsedModelResponse:
        choices = value.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ExperimentalCallError("provider choices are missing")
        first = choices[0]
        if not isinstance(first, Mapping):
            raise ExperimentalCallError("provider choice is invalid")
        message = first.get("message")
        if not isinstance(message, Mapping):
            raise ExperimentalCallError("provider message is missing")
        content = message.get("content")
        if not isinstance(content, str):
            raise ExperimentalCallError("provider text content is missing")
        usage = value.get("usage") or {}
        if not isinstance(usage, Mapping):
            raise ExperimentalCallError("provider usage is invalid")
        details = usage.get("prompt_tokens_details") or {}
        if not isinstance(details, Mapping):
            details = {}
        return ParsedModelResponse(
            content=content,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cached_input_tokens=int(details.get("cached_tokens") or 0),
        )


class LocalHTTPAdapter(BaseExperimentalHTTPAdapter):
    PLUGIN_ID = "local-http"
    def build_payload(
        self, request: ExternalExecutionRequest
    ) -> Mapping[str, Any]:
        return {
            "model": self.config.model_id,
            "prompt": canonical_json(_prompt_contract(request.work_order)),
            "max_output_tokens": request.max_output_tokens,
            "reasoning_effort": self.config.reasoning_effort,
        }

    def parse_response(
        self, value: Mapping[str, Any]
    ) -> ParsedModelResponse:
        content = value.get("output", value.get("response", value.get("text")))
        if not isinstance(content, str):
            raise ExperimentalCallError("local response text is missing")
        usage = value.get("usage") or {}
        if not isinstance(usage, Mapping):
            raise ExperimentalCallError("local usage is invalid")
        return ParsedModelResponse(
            content=content,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cached_input_tokens=int(usage.get("cached_input_tokens") or 0),
        )


__all__ = [
    "AdapterRuntimePolicy",
    "AdapterTransportError",
    "ExperimentalAdapterConfig",
    "ExternalCallReceipt",
    "ExternalExecutionRequest",
    "HTTPResponse",
    "HTTPTransport",
    "HTTP_ADAPTER_SCHEMA_VERSION",
    "LOCAL_HTTP_ADAPTER_REVISION",
    "LocalHTTPAdapter",
    "OPENAI_COMPATIBLE_ADAPTER_REVISION",
    "OpenAICompatibleHTTPAdapter",
    "ParsedModelResponse",
    "UrllibHTTPTransport",
]
