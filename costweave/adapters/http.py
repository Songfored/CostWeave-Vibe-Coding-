"""Default-off, capability-scoped HTTP Adapter plugins for v0.5.8.2."""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import multiprocessing
import os
import pickle
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

from ..version import __version__
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
MAX_CONCURRENT_DNS_RESOLVERS = 4
_DNS_RESOLVER_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT_DNS_RESOLVERS)

SECRET_LIKE = re.compile(
    r"(?i)(?:bearer\s+|api[_-]?key[=: ]+|sk-)[A-Za-z0-9._~+/-]{6,}"
)


def _dns_resolution_child(url: str, resolver_payload: bytes, sender: Any) -> None:
    try:
        resolver = pickle.loads(resolver_payload)
        sender.send(
            (True, validate_remote_resolution(url, resolver=resolver))
        )
    except BaseException:
        try:
            sender.send((False, None))
        except (BrokenPipeError, EOFError, OSError):
            pass
    finally:
        sender.close()


def _stop_dns_process(process: multiprocessing.Process) -> bool:
    process.join(0.1)
    if process.is_alive():
        process.terminate()
        process.join(1.0)
    if process.is_alive() and hasattr(process, "kill"):
        process.kill()
        process.join(1.0)
    return not process.is_alive()


class AdapterTransportError(ExperimentalCallError):
    """Categorical transport failure with no response content."""


def _remaining_seconds(deadline_monotonic: float) -> float:
    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise AdapterTransportError("absolute_timeout")
    return max(0.001, remaining)


def _set_stream_timeout(stream: Any, timeout_seconds: float) -> None:
    """Best-effort timeout update for urllib/http.client response wrappers."""

    current = stream
    visited: set[int] = set()
    for _ in range(6):
        if current is None or id(current) in visited:
            return
        visited.add(id(current))
        setter = getattr(current, "settimeout", None)
        if callable(setter):
            setter(timeout_seconds)
            return
        next_item = None
        for name in ("fp", "raw", "_sock", "sock"):
            candidate = getattr(current, name, None)
            if candidate is not None:
                next_item = candidate
                break
        current = next_item


def _read_with_deadline(
    stream: Any, *, maximum_bytes: int, deadline_monotonic: float
) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        remaining = _remaining_seconds(deadline_monotonic)
        _set_stream_timeout(stream, remaining)
        size = min(64 * 1024, maximum_bytes + 1 - total)
        if size <= 0:
            raise AdapterTransportError("response_too_large")
        reader = getattr(stream, "read1", None)
        chunk = reader(size) if callable(reader) else stream.read(size)
        if not chunk:
            return b"".join(chunks)
        chunks.append(bytes(chunk))
        total += len(chunk)
        if total > maximum_bytes:
            raise AdapterTransportError("response_too_large")


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
    input_reservation_multiplier: float = 1.0
    billing_header: str | None = None
    adapter_revision: str = OPENAI_COMPATIBLE_ADAPTER_REVISION
    accounting_mode: str = "priced"

    def __post_init__(self) -> None:
        if self.accounting_mode not in {"priced", "usage_only"}:
            raise ExperimentalCallError("invalid accounting mode")
        if self.accounting_mode == "usage_only" and (
            self.input_price_per_mtok_usd != 0 or self.output_price_per_mtok_usd != 0
            or self.billing_header is not None or self.max_retries != 0
        ):
            raise ExperimentalCallError("usage-only accounting requires unknown prices and zero retries")
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
        if not all(
            math.isfinite(float(value))
            for value in (
                self.input_price_per_mtok_usd,
                self.output_price_per_mtok_usd,
                self.timeout_seconds,
                self.input_reservation_multiplier,
            )
        ):
            raise ExperimentalCallError(
                "adapter pricing and timeout must be finite"
            )
        if min(
            self.input_price_per_mtok_usd,
            self.output_price_per_mtok_usd,
            self.timeout_seconds,
        ) < 0:
            raise ExperimentalCallError("adapter pricing or timeout is invalid")
        if self.timeout_seconds <= 0 or not 0 <= self.max_retries <= 3:
            raise ExperimentalCallError("bounded adapter retry policy is required")
        if not 1.0 <= self.input_reservation_multiplier <= 16.0:
            raise ExperimentalCallError(
                "input reservation multiplier must be between 1 and 16"
            )
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
            "input_reservation_multiplier": self.input_reservation_multiplier,
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
    structured_output: bool = False

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
        if not isinstance(self.structured_output, bool):
            raise ExperimentalCallError("structured_output must be a boolean")
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
        deadline_monotonic: float | None = None,
    ) -> None:
        super().__init__(hostname, port, timeout=timeout, context=context)
        self._approved_addresses = frozenset(approved_addresses)
        self._pinned_context = context
        self._deadline_monotonic = (
            deadline_monotonic
            if deadline_monotonic is not None
            else time.monotonic() + timeout
        )
        self._pending_socket: Any | None = None

    def connect(self) -> None:
        last_error: OSError | None = None
        for address in sorted(self._approved_addresses):
            raw_socket = None
            try:
                remaining = _remaining_seconds(self._deadline_monotonic)
                raw_socket = socket.create_connection(
                    (address, self.port), timeout=remaining
                )
                self._pending_socket = raw_socket
                if hasattr(raw_socket, "settimeout"):
                    raw_socket.settimeout(
                        _remaining_seconds(self._deadline_monotonic)
                    )
                wrapped = self._pinned_context.wrap_socket(
                    raw_socket, server_hostname=self.host
                )
                self._pending_socket = wrapped
                if hasattr(wrapped, "settimeout"):
                    wrapped.settimeout(
                        _remaining_seconds(self._deadline_monotonic)
                    )
                peer = str(wrapped.getpeername()[0]).split("%", 1)[0]
                if peer not in self._approved_addresses:
                    wrapped.close()
                    raise OSError("connected peer is outside the approved DNS set")
                self.sock = wrapped
                self._pending_socket = None
                return
            except OSError as error:
                last_error = error
                if raw_socket is not None:
                    try:
                        raw_socket.close()
                    except OSError:
                        pass
                self._pending_socket = None
        raise OSError("no approved remote address could be connected") from last_error

    def close(self) -> None:
        pending = self._pending_socket
        self._pending_socket = None
        if pending is not None:
            try:
                pending.close()
            except OSError:
                pass
        super().close()


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
        if not callable(resolver):
            raise AdapterTransportError("dns_resolver_not_callable")
        try:
            # Serialize at trusted transport construction time.  The request
            # path only passes inert bytes into the child, so a resolver with
            # a hostile or blocking ``__reduce__`` cannot run while an HTTP
            # deadline or project mutation boundary is held.
            self._resolver_payload = pickle.dumps(
                resolver, protocol=pickle.HIGHEST_PROTOCOL
            )
        except Exception as error:
            raise AdapterTransportError(
                "dns_resolver_not_isolatable"
            ) from error
        self._ssl_context_factory = ssl_context_factory
        self._pinned_connection_class = pinned_connection_class

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    def _resolve_remote(
        self, url: str, *, deadline_monotonic: float
    ) -> tuple[str, ...]:
        if not _DNS_RESOLVER_SLOTS.acquire(
            timeout=_remaining_seconds(deadline_monotonic)
        ):
            raise AdapterTransportError("absolute_timeout")
        process: multiprocessing.Process | None = None
        receiver = None
        sender = None
        release_slot = True
        try:
            context = multiprocessing.get_context("spawn")
            receiver, sender = context.Pipe(duplex=False)
            process = context.Process(
                target=_dns_resolution_child,
                args=(url, self._resolver_payload, sender),
                name="costweave-dns-resolver",
                daemon=True,
            )
            process.start()
            sender.close()
            sender = None
            if not receiver.poll(_remaining_seconds(deadline_monotonic)):
                if not _stop_dns_process(process):
                    release_slot = False
                    raise AdapterTransportError("dns_resolver_isolation_failed")
                raise AdapterTransportError("absolute_timeout")
            try:
                succeeded, value = receiver.recv()
            except (EOFError, OSError) as error:
                raise AdapterTransportError("transport_error") from error
            if not _stop_dns_process(process):
                release_slot = False
                raise AdapterTransportError("dns_resolver_isolation_failed")
            if not succeeded:
                raise AdapterTransportError("transport_error")
            return tuple(value)
        except AdapterTransportError:
            raise
        except Exception as error:
            if process is not None and process.pid is not None:
                if not _stop_dns_process(process):
                    release_slot = False
            raise AdapterTransportError("transport_error") from error
        finally:
            try:
                if process is not None and process.pid is not None:
                    # Deadline expiry during process.start() can bypass poll
                    # and its normal reap path. Never return that child's slot
                    # until termination is confirmed, even on an early error.
                    release_slot = False
                    if process.is_alive():
                        _stop_dns_process(process)
                    if not process.is_alive():
                        process.close()
                        release_slot = True
            finally:
                try:
                    if sender is not None:
                        sender.close()
                finally:
                    try:
                        if receiver is not None:
                            receiver.close()
                    finally:
                        if release_slot:
                            _DNS_RESOLVER_SLOTS.release()

    def _post_remote_pinned(
        self,
        *,
        url: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout_seconds: float,
        method: str = "POST",
    ) -> HTTPResponse:
        parsed = urlparse(url)
        deadline = time.monotonic() + timeout_seconds
        addresses = self._resolve_remote(
            url, deadline_monotonic=deadline
        )
        connection = self._pinned_connection_class(
            str(parsed.hostname),
            int(parsed.port or 443),
            addresses,
            timeout=_remaining_seconds(deadline),
            context=self._ssl_context_factory(),
            deadline_monotonic=deadline,
        )
        started = time.monotonic()
        timer = threading.Timer(
            _remaining_seconds(deadline), connection.close
        )
        timer.daemon = True
        timer.start()
        try:
            connection.request(
                method,
                parsed.path or "/",
                body=body,
                headers=dict(headers),
            )
            if getattr(connection, "sock", None) is not None:
                connection.sock.settimeout(_remaining_seconds(deadline))
            response = connection.getresponse()
            payload = _read_with_deadline(
                response,
                maximum_bytes=MAX_RESPONSE_BYTES,
                deadline_monotonic=deadline,
            )
            return HTTPResponse(
                status_code=int(response.status),
                body=payload,
                headers=dict(response.getheaders()),
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        except AdapterTransportError:
            raise
        except Exception as error:
            if time.monotonic() >= deadline:
                raise AdapterTransportError("absolute_timeout") from error
            raise AdapterTransportError("transport_error") from error
        finally:
            timer.cancel()
            connection.close()

    def get_models(self, *, url: str, headers: Mapping[str, str]) -> HTTPResponse:
        """Bounded metadata GET; shares pinned DNS, TLS and no-redirect transport."""
        validate_endpoint(url, deployment="remote")
        return self._post_remote_pinned(url=url, headers=headers, body=b"",
                                        timeout_seconds=15, method="GET")

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
        parsed = urlparse(url)
        deadline = time.monotonic() + timeout_seconds
        connection_class = (
            http.client.HTTPSConnection
            if parsed.scheme == "https"
            else http.client.HTTPConnection
        )
        connection = connection_class(
            str(parsed.hostname),
            int(parsed.port or (443 if parsed.scheme == "https" else 80)),
            timeout=_remaining_seconds(deadline),
        )
        started = time.monotonic()
        timer = threading.Timer(
            _remaining_seconds(deadline), connection.close
        )
        timer.daemon = True
        timer.start()
        try:
            connection.request(
                "POST", parsed.path or "/", body=body, headers=dict(headers)
            )
            if connection.sock is not None:
                connection.sock.settimeout(_remaining_seconds(deadline))
            response = connection.getresponse()
            payload = _read_with_deadline(
                response,
                maximum_bytes=MAX_RESPONSE_BYTES,
                deadline_monotonic=deadline,
            )
            return HTTPResponse(
                status_code=int(response.status),
                body=payload,
                headers=dict(response.getheaders()),
                elapsed_ms=round((time.monotonic() - started) * 1000),
            )
        except AdapterTransportError:
            raise
        except Exception as error:
            if time.monotonic() >= deadline:
                raise AdapterTransportError("absolute_timeout") from error
            raise AdapterTransportError("transport_error") from error
        finally:
            timer.cancel()
            connection.close()


@dataclass(frozen=True, slots=True)
class ParsedModelResponse:
    content: str
    input_tokens: int
    output_tokens: int
    cached_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    reasoning_tokens: int = 0
    reasoning_content_present: bool = False
    finish_reason: str | None = None
    provider_response_id: str | None = None
    reported_model: str | None = None

    def __post_init__(self) -> None:
        if min(
            self.input_tokens,
            self.output_tokens,
            self.cached_input_tokens,
            self.cache_read_input_tokens,
            self.cache_creation_input_tokens,
            self.reasoning_tokens,
        ) < 0:
            raise ExperimentalCallError("provider token usage is invalid")
        if self.cache_read_input_tokens == 0 and self.cached_input_tokens:
            object.__setattr__(
                self, "cache_read_input_tokens", self.cached_input_tokens
            )
        elif self.cached_input_tokens == 0 and self.cache_read_input_tokens:
            object.__setattr__(
                self, "cached_input_tokens", self.cache_read_input_tokens
            )
        elif self.cached_input_tokens != self.cache_read_input_tokens:
            raise ExperimentalCallError(
                "cached input and cache-read token fields disagree"
            )
        for value, name in (
            (self.finish_reason, "provider finish reason"),
            (self.provider_response_id, "provider response id"),
            (self.reported_model, "provider reported model"),
        ):
            if value is not None and (
                not str(value).strip() or len(str(value)) > 512
            ):
                raise ExperimentalCallError(f"{name} is invalid")


@dataclass(frozen=True, slots=True)
class ExternalCallReceipt:
    external_call_id: str
    result: ResultEnvelope
    telemetry: Mapping[str, Any]
    replayed: bool = False
    schema_version: str = HTTP_ADAPTER_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        value = {
            "external_call_id": self.external_call_id,
            "result": self.result.to_dict(),
            "telemetry": dict(self.telemetry),
            "replayed": self.replayed,
            "schema_version": self.schema_version,
        }
        if self.telemetry.get("accounting_mode") == "usage_only":
            value["result"]["actual_cost"] = None
        return value

    @classmethod
    def from_dict(
        cls, value: Mapping[str, Any], *, replayed: bool
    ) -> "ExternalCallReceipt":
        result = dict(value["result"])
        if result.get("actual_cost") is None and value["telemetry"].get("accounting_mode") == "usage_only":
            result["actual_cost"] = 0.0  # Legacy in-memory envelope only; never serialize as a known cost.
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
        message = str(error).lower()
        if "contained current leased secret material" in message:
            return "secret_exposure_detected"
        if "closed or expired" in message:
            return "secret_lease_expired"
        if "unavailable" in message:
            return "secret_unavailable"
        if any(
            marker in message
            for marker in (
                "inspection",
                "normalization",
                "could not be checked",
            )
        ):
            return "secret_inspection_failed"
        return "secret_lease_failed"
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
            "User-Agent": f"CostWeave/{__version__}-experimental",
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
            * self.config.input_reservation_multiplier
            / 1_000_000
            * self.config.input_price_per_mtok_usd
            + max_output_tokens
            / 1_000_000
            * self.config.output_price_per_mtok_usd,
            10,
        )

    def estimate_reservation(
        self, request: ExternalExecutionRequest
    ) -> dict[str, int | float]:
        """Return the exact local reservation inputs without admitting a call.

        The estimator deliberately reuses ``build_payload`` and the same
        UTF-8-byte upper bound as execution.  It performs no secret lookup,
        DNS resolution, budget mutation or network access, so callers can
        size a WorkOrder before deciding whether the call is affordable.
        """

        payload_raw = canonical_json(self.build_payload(request)).encode("utf-8")
        input_upper = len(payload_raw)
        per_attempt = self._worst_case_cost(
            input_upper, request.max_output_tokens
        )
        attempts = self.config.max_retries + 1
        return {
            "input_token_upper_bound": input_upper,
            "max_output_tokens": request.max_output_tokens,
            "input_reservation_multiplier": self.config.input_reservation_multiplier,
            "worst_case_cost_per_attempt_usd": per_attempt,
            "max_attempts": attempts,
            "required_work_order_cost_limit_usd": round(
                per_attempt * attempts, 10
            ),
        }

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
                if not math.isfinite(billed) or billed < 0:
                    raise ExperimentalCallError(
                        "provider billing header must be finite and non-negative"
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
        # Long model calls share a project session.  Revocation owns the
        # session's exclusive side and therefore still waits for transport,
        # capability cleanup and secret-lease closure, while independent calls
        # no longer serialize behind network latency.  Durable mutations keep
        # using the separate short-lived active_project_operation boundary.
        with self.security_store.active_project_session(request.project_id):
            secret_lease: SecretLease | None = None
            try:
                # Wait for any authorized durable mutation before issuing the
                # short-lived secret.  Only admission is serialized here;
                # transport and response inspection remain concurrent.
                with self.security_store.active_project_operation(
                    request.project_id
                ):
                    secret_lease, prompt_assessment, manifest = self._admit(request)
                try:
                    return self._execute_admitted(
                        request,
                        secret_lease=secret_lease,
                        prompt_assessment=prompt_assessment,
                        manifest=manifest,
                    )
                finally:
                    # Capabilities must be gone before the project operation
                    # lock is released, so revocation cannot return while a
                    # previously issued transport authority remains usable.
                    self.authority.close_capabilities_for_request(
                        project_id=request.project_id,
                        data_residency=request.data_residency,
                        run_id=request.run_id,
                        task_id=request.task_id,
                    )
            finally:
                if secret_lease is not None:
                    secret_lease.close()

    def _replay_completed_side_effect(
        self,
        *,
        reservation: Any,
        side_effect_id: str,
        project_id: str,
        data_residency: str,
        secret_lease: SecretLease | None,
    ) -> ExternalCallReceipt | None:
        state = self.security_store.side_effect(side_effect_id) or {}
        contract = state.get("contract")
        if not isinstance(contract, Mapping):
            return None
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
        source_call_id = str(contract.get("origin_external_call_id") or "")
        source_call = self.store.call(source_call_id) if source_call_id else None
        expected = {
            "external_call_id": source_call_id,
            "project_id": normalize_project_id(project_id),
            "data_residency": normalize_data_residency(data_residency),
            "adapter_id": self.config.adapter_id,
            "provider": self.config.provider,
            "deployment_id": self.config.deployment_id,
            "endpoint": self.config.endpoint,
            "model_id": self.config.model_id,
            "adapter_revision": self.config.adapter_revision,
            "operation": "model_inference",
            "request_hash": contract.get("request_hash"),
            "provider_idempotency_key_hash": contract.get(
                "provider_idempotency_key_hash"
            ),
            "side_effect_id": side_effect_id,
        }
        if (
            source_call is None
            or source_call.get("status") != "completed"
            or any(
                source_call.get(key) != contract.get(key)
                for key in (
                    "provider",
                    "deployment_id",
                    "model_id",
                    "adapter_revision",
                )
            )
            or completed.get("detail", {}).get("external_call_id")
            != source_call_id
        ):
            raise ExperimentalCallError(
                "completed side-effect replay source is not authoritative"
            )
        source_result = source_call.get("result") if source_call is not None else None
        if not isinstance(source_result, Mapping):
            return None
        if secret_lease is not None:
            secret_lease.reject_if_exposed(source_result)
        prior = ExternalCallReceipt.from_dict(source_result, replayed=True)
        if any(prior.telemetry.get(key) != value for key, value in expected.items()):
            raise ExperimentalCallError(
                "completed side-effect replay receipt does not match its contract"
            )
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
        if prior.telemetry.get("accounting_mode") == "usage_only":
            telemetry["actual_cost_usd"] = None
        receipt = ExternalCallReceipt(
            external_call_id=reservation.external_call_id,
            result=replayed_result,
            telemetry=telemetry,
            replayed=True,
        )
        if secret_lease is not None:
            secret_lease.reject_if_exposed(receipt.to_dict())
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
        estimate = self.estimate_reservation(request)
        input_upper = int(estimate["input_token_upper_bound"])
        worst_per_attempt = float(
            estimate["worst_case_cost_per_attempt_usd"]
        )
        if (
            worst_per_attempt * (self.config.max_retries + 1)
            > request.work_order.cost_limit + 1e-10
        ):
            raise ExternalBudgetExceededError(
                "WorkOrder hard cost limit cannot fund worst-case attempts"
            )
        payload_hash = hashlib.sha256(payload_raw).hexdigest()
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
            "provider_profile_revision": getattr(
                self, "PROFILE_REVISION", self.config.adapter_revision
            ),
            "adapter_id": self.config.adapter_id,
            "operation": "model_inference",
            "endpoint": self.config.endpoint,
            "request_hash": payload_hash,
            "provider_idempotency_key_hash": hashlib.sha256(
                provider_idempotency_key.encode("utf-8")
            ).hexdigest(),
            "max_output_tokens": request.max_output_tokens,
            "payload_hash": hashlib.sha256(payload_raw).hexdigest(),
        }
        if self.config.accounting_mode == "usage_only":
            fingerprint["accounting_mode"] = "usage_only"
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
                "accounting_mode": self.config.accounting_mode,
            },
            project_id=request.project_id,
            data_residency=request.data_residency,
            adapter_id=self.config.adapter_id,
            provider=self.config.provider,
            deployment_id=self.config.deployment_id,
            endpoint=self.config.endpoint,
            model_id=self.config.model_id,
            adapter_revision=self.config.adapter_revision,
            operation="model_inference",
            provider_idempotency_key=provider_idempotency_key,
            request_hash=payload_hash,
            max_attempts=self.config.max_retries + 1,
        )
        reservation = authority_reservation.logical
        if authority_reservation.mode == "logical_replay":
            if reservation.status == "completed" and reservation.result:
                if secret_lease is not None:
                    secret_lease.reject_if_exposed(reservation.result)
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
                project_id=request.project_id,
                data_residency=request.data_residency,
                secret_lease=secret_lease,
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
        provider_response_ids: list[str] = []
        reported_models: list[str] = []
        finish_reasons: list[str] = []
        reasoning_tokens = 0
        reasoning_only_responses = 0
        cache_read_input_tokens = 0
        cache_creation_input_tokens = 0
        evidence_level = str(
            getattr(
                self.transport,
                "evidence_level",
                "E0-transport-fixture",
            )
        )
        result: ResultEnvelope | None = None
        last_error = "external_call_failed"
        call_deadline = time.monotonic() + self.config.timeout_seconds
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
                    timeout_seconds=_remaining_seconds(call_deadline),
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
                if secret_lease is not None:
                    secret_lease.reject_if_exposed(value)
                parsed = self.parse_response(value)
                if parsed.provider_response_id:
                    provider_response_ids.append(parsed.provider_response_id)
                if parsed.reported_model:
                    reported_models.append(parsed.reported_model)
                if parsed.finish_reason:
                    finish_reasons.append(parsed.finish_reason)
                reasoning_tokens += parsed.reasoning_tokens
                cache_read_input_tokens += parsed.cache_read_input_tokens
                cache_creation_input_tokens += (
                    parsed.cache_creation_input_tokens
                )
                if parsed.reasoning_content_present and not parsed.content.strip():
                    reasoning_only_responses += 1
                if secret_lease is not None:
                    secret_lease.reject_if_exposed(parsed.content)
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
            "provider_profile_revision": getattr(
                self, "PROFILE_REVISION", self.config.adapter_revision
            ),
            "adapter_id": self.config.adapter_id,
            "operation": "model_inference",
            "endpoint": self.config.endpoint,
            "request_hash": payload_hash,
            "provider_idempotency_key_hash": hashlib.sha256(
                provider_idempotency_key.encode("utf-8")
            ).hexdigest(),
            "reasoning_effort": self.config.reasoning_effort,
            "attempt_count": len(attempts),
            "input_tokens": sum(item.input_tokens for item in attempts),
            "output_tokens": sum(item.output_tokens for item in attempts),
            "cached_input_tokens": sum(
                item.cached_input_tokens for item in attempts
            ),
            "cache_read_input_tokens": cache_read_input_tokens,
            "cache_creation_input_tokens": cache_creation_input_tokens,
            "estimated_cache_read_cost_cny": None,
            "estimated_cache_creation_cost_cny": None,
            "provider_billed_cache_read_cost_cny": None,
            "provider_billed_cache_creation_cost_cny": None,
            "estimated_billed_cost_cny": None,
            "provider_billed_cost_cny": None,
            "billing_residual_cny": None,
            "billing_currency": "CNY",
            "invoice_reconciled": False,
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
            "provider_response_ids": list(dict.fromkeys(provider_response_ids)),
            "reported_models": list(dict.fromkeys(reported_models)),
            "finish_reasons": list(dict.fromkeys(finish_reasons)),
            "reasoning_tokens": reasoning_tokens,
            "reasoning_only_response_count": reasoning_only_responses,
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
        if self.config.accounting_mode == "usage_only":
            telemetry.update(accounting_mode="usage_only", actual_cost_usd=None,
                             billing_currency=None, billing_sources=["awaiting_provider_invoice"],
                             monetary_budget_enforced=False)
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
        # The provider-controlled response body and parsed content were already
        # inspected before the completed attempt was recorded (lines 1146 and
        # 1205-1218 above).  Re-normalizing the complete internal receipt here
        # is both redundant and unsafe operationally: generated SHA-256 values,
        # IDs and telemetry can look like nested base64/hex payloads and exhaust
        # the bounded secret scanner after the provider has already charged us.
        # That used to strand a successful call in `reserved`.  Only untrusted
        # provider material is inspected; the system envelope adds no new
        # provider-controlled bytes.
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
    PROFILE_REVISION = OPENAI_COMPATIBLE_ADAPTER_REVISION
    def build_payload(
        self, request: ExternalExecutionRequest
    ) -> Mapping[str, Any]:
        payload: dict[str, Any] = {
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
        if request.structured_output:
            payload["response_format"] = {"type": "json_object"}
        return payload

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
        reasoning_content = message.get("reasoning_content")
        if content is None and isinstance(reasoning_content, str):
            content = ""
        if not isinstance(content, str):
            raise ExperimentalCallError("provider text content is missing")
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            raise ExperimentalCallError("provider reasoning content is invalid")
        usage = value.get("usage") or {}
        if not isinstance(usage, Mapping):
            raise ExperimentalCallError("provider usage is invalid")
        details = usage.get("prompt_tokens_details") or {}
        if not isinstance(details, Mapping):
            details = {}
        completion_details = usage.get("completion_tokens_details") or {}
        if not isinstance(completion_details, Mapping):
            completion_details = {}
        finish_reason = first.get("finish_reason")
        if finish_reason is not None:
            finish_reason = str(finish_reason)
        cache_read_tokens = int(
            details.get("cached_tokens")
            or details.get("cache_read_tokens")
            or usage.get("cache_read_input_tokens")
            or usage.get("prompt_cache_hit_tokens")
            or usage.get("input_cache_read_tokens")
            or 0
        )
        cache_creation_tokens = int(
            details.get("cache_creation_tokens")
            or details.get("cache_write_tokens")
            or usage.get("cache_creation_input_tokens")
            or usage.get("cache_write_input_tokens")
            or usage.get("prompt_cache_write_tokens")
            or usage.get("input_cache_creation_tokens")
            or 0
        )
        return ParsedModelResponse(
            content=content,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cached_input_tokens=cache_read_tokens,
            cache_read_input_tokens=cache_read_tokens,
            cache_creation_input_tokens=cache_creation_tokens,
            reasoning_tokens=int(completion_details.get("reasoning_tokens") or 0),
            reasoning_content_present=bool(reasoning_content),
            finish_reason=finish_reason,
            provider_response_id=(
                str(value.get("id")) if value.get("id") is not None else None
            ),
            reported_model=(
                str(value.get("model"))
                if value.get("model") is not None
                else None
            ),
        )


class DeepSeekChatHTTPAdapter(OpenAICompatibleHTTPAdapter):
    """DeepSeek Chat Completions profile for bounded structured work orders.

    DeepSeek thinking mode returns chain-of-thought separately from final
    ``content``.  Small structured nodes previously spent their entire output
    allowance on reasoning and therefore produced an empty final artifact.
    Low-effort work orders now use non-thinking JSON mode; higher efforts keep
    thinking enabled and continue to account for reasoning tokens separately.
    """

    PROFILE_REVISION = "deepseek-chat-json-v1"

    def build_payload(
        self, request: ExternalExecutionRequest
    ) -> Mapping[str, Any]:
        payload = dict(super().build_payload(request))
        messages = [dict(message) for message in payload["messages"]]
        messages[0]["content"] = (
            str(messages[0]["content"])
            + " Return exactly one valid JSON object matching the supplied "
            "output schema. Do not use Markdown fences or explanatory text."
        )
        required = request.work_order.output_schema.get("required", ())
        example = {
            str(key): None
            for key in required
            if isinstance(key, str) and key.strip()
        }
        contract = json.loads(str(messages[1]["content"]))
        if example:
            contract["json_output_example"] = example
        messages[1]["content"] = canonical_json(contract)
        payload["messages"] = messages
        payload["response_format"] = {"type": "json_object"}

        effort = self.config.reasoning_effort.strip().lower()
        if effort in {"none", "minimal", "low"}:
            payload["thinking"] = {"type": "disabled"}
            payload.pop("reasoning_effort", None)
        else:
            payload["thinking"] = {"type": "enabled"}
            payload["reasoning_effort"] = {
                "medium": "high",
                "xhigh": "high",
                "ultra": "max",
            }.get(effort, effort if effort in {"high", "max"} else "high")
        return payload


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
            cache_read_input_tokens=int(
                usage.get("cache_read_input_tokens")
                or usage.get("cached_input_tokens")
                or 0
            ),
            cache_creation_input_tokens=int(
                usage.get("cache_creation_input_tokens") or 0
            ),
            provider_response_id=(
                str(value.get("id")) if value.get("id") is not None else None
            ),
            reported_model=(
                str(value.get("model"))
                if value.get("model") is not None
                else None
            ),
        )


__all__ = [
    "AdapterRuntimePolicy",
    "DeepSeekChatHTTPAdapter",
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
