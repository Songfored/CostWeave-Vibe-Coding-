"""Central fail-closed security contracts for CostWeave v0.5.8.2."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import socket
import threading
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlparse


SECURITY_SCHEMA_VERSION = "v0.5.8.2"
SECURITY_POLICY_REVISION = "central-security-boundary-1"
DEFAULT_PROJECT_ID = "local-default"
DEFAULT_DATA_RESIDENCY = "local"
MAX_SECRET_BYTES = 8_192
MAX_RESPONSE_BYTES = 4_000_000

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{2,63}$")
_RESIDENCY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,31}$")
_ENV_RE = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9.-]{1,253}$")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "authorization",
        "proxy_authorization",
        "bearer",
        "credential",
        "credentials",
        "password",
        "passwd",
        "secret",
        "signing_key",
        "private_key",
        "client_secret",
        "cookie",
        "set_cookie",
    }
)
_VALUE_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{6,}"),
    re.compile(r"(?i)\b(?:sk|rk|pk)-[A-Za-z0-9._~+/-]{6,}"),
    re.compile(
        r"(?i)(\b(?:api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"client[_-]?secret|password|passwd|secret|signature)\b\s*[=:]\s*)"
        r"([^\s,;\"'&]{4,})"
    ),
    re.compile(
        r"(?i)([?&](?:api[_-]?key|access[_-]?token|token|signature|"
        r"client[_-]?secret)=)([^&#\s]{4,})"
    ),
)

_PROMPT_MARKERS = (
    (
        "instruction_override",
        re.compile(
            r"(?i)(ignore|disregard|override|bypass).{0,48}"
            r"(previous|prior|system|instruction|policy|safety|permission)"
            r"|忽略.{0,24}(此前|之前|系统|指令|策略|权限)",
        ),
    ),
    (
        "secret_exfiltration",
        re.compile(
            r"(?i)(reveal|print|show|return|exfiltrat|steal).{0,48}"
            r"(api.?key|secret|password|credential|token|environment)"
            r"|(?:泄露|显示|输出|窃取).{0,24}(密钥|密码|凭据|令牌|环境变量)",
        ),
    ),
    (
        "tool_or_network_override",
        re.compile(
            r"(?i)(invoke|call|execute|enable|grant).{0,48}"
            r"(shell|tool|network|database|filesystem|permission)"
            r"|(?:调用|执行|启用|授予).{0,24}(工具|网络|数据库|文件|权限)",
        ),
    ),
)


class SecurityBoundaryError(ValueError):
    """Base error for fail-closed security admission."""


class ProjectScopeError(SecurityBoundaryError):
    """Raised when a project or residency identifier is unsafe."""


class SecretLeaseError(SecurityBoundaryError):
    """Raised before secret material can reach an Adapter."""


class AdapterAdmissionError(SecurityBoundaryError):
    """Raised before a network or tool capability is granted."""


class PromptInjectionError(SecurityBoundaryError):
    """Raised when untrusted instructions need separate approval."""


def normalize_project_id(value: str | None) -> str:
    project_id = str(value or DEFAULT_PROJECT_ID).strip().lower()
    if not _PROJECT_RE.fullmatch(project_id):
        raise ProjectScopeError(
            "project id must contain 3-64 lowercase letters, digits, dots, underscores or hyphens"
        )
    return project_id


def normalize_data_residency(value: str | None) -> str:
    residency = str(value or DEFAULT_DATA_RESIDENCY).strip().lower()
    if not _RESIDENCY_RE.fullmatch(residency):
        raise ProjectScopeError("data residency identifier is invalid")
    return residency


def _sensitive_key(key: object) -> bool:
    normalised = str(key).strip().lower().replace("-", "_")
    return (
        normalised in _SENSITIVE_KEYS
        or normalised.endswith("_api_key")
        or normalised.endswith("_password")
        or normalised.endswith("_secret")
        or normalised.endswith("_access_token")
        or normalised.endswith("_private_key")
    )


def redact_text(value: str) -> str:
    redacted = str(value)
    for pattern in _VALUE_PATTERNS:
        if pattern.groups >= 2:
            redacted = pattern.sub(lambda match: match.group(1) + "[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted


def redact_structure(value: Any, *, _depth: int = 0) -> Any:
    """Return a JSON-safe copy with structural and inline secrets removed."""

    if _depth > 48:
        return "[REDACTED:DEPTH_LIMIT]"
    if isinstance(value, Mapping):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            text_key = str(key)
            safe_key = redact_text(text_key)
            cleaned[safe_key] = (
                "[REDACTED]"
                if _sensitive_key(text_key)
                else redact_structure(item, _depth=_depth + 1)
            )
        return cleaned
    if isinstance(value, (list, tuple, set, frozenset)):
        return [redact_structure(item, _depth=_depth + 1) for item in value]
    if isinstance(value, bytes):
        return "[REDACTED:BINARY]"
    if isinstance(value, str):
        return redact_text(value)
    return value


def contains_secret_material(value: Any) -> bool:
    if isinstance(value, Mapping):
        return any(
            _sensitive_key(key) or contains_secret_material(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(contains_secret_material(item) for item in value)
    if isinstance(value, bytes):
        return True
    if isinstance(value, str):
        return redact_text(value) != value
    return False


@dataclass(frozen=True, slots=True)
class PromptSecurityAssessment:
    risk: str
    markers: tuple[str, ...]
    approval_required: bool
    untrusted_fields: tuple[str, ...] = ("objective", "assumptions")
    schema_version: str = SECURITY_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk": self.risk,
            "markers": list(self.markers),
            "approval_required": self.approval_required,
            "untrusted_fields": list(self.untrusted_fields),
            "schema_version": self.schema_version,
        }


def assess_prompt_security(*parts: object) -> PromptSecurityAssessment:
    joined = "\n".join(str(part) for part in parts if part is not None)
    markers = tuple(name for name, pattern in _PROMPT_MARKERS if pattern.search(joined))
    if contains_secret_material(joined):
        markers = tuple(dict.fromkeys((*markers, "secret_material")))
    high_markers = {
        "secret_exfiltration",
        "tool_or_network_override",
        "secret_material",
    }
    risk = (
        "high"
        if any(marker in high_markers for marker in markers) or len(markers) >= 2
        else "medium"
        if markers
        else "low"
    )
    return PromptSecurityAssessment(
        risk=risk,
        markers=markers,
        approval_required=risk in {"medium", "high"},
    )


def require_prompt_admission(
    assessment: PromptSecurityAssessment,
    *,
    approval_id: str | None,
) -> None:
    if "secret_material" in assessment.markers:
        raise PromptInjectionError(
            "detected secret material is never admitted to an Adapter prompt"
        )
    if not assessment.approval_required:
        return
    if not approval_id or not re.fullmatch(r"approval_[A-Za-z0-9._:-]{8,120}", approval_id):
        raise PromptInjectionError(
            "untrusted prompt override or exfiltration pattern requires a separate security approval"
        )


@dataclass(frozen=True, slots=True)
class SecretReference:
    env_name: str
    version_env: str | None = None

    def __post_init__(self) -> None:
        if not _ENV_RE.fullmatch(self.env_name):
            raise SecretLeaseError("secret environment reference is invalid")
        if self.version_env is not None and not _ENV_RE.fullmatch(self.version_env):
            raise SecretLeaseError("secret version environment reference is invalid")


class SecretLease:
    """Short-lived, non-serializable secret bytes with explicit zeroing."""

    __slots__ = (
        "adapter_id",
        "reference",
        "version",
        "issued_at_monotonic",
        "expires_at_monotonic",
        "_secret",
        "_closed",
    )

    def __init__(
        self,
        *,
        adapter_id: str,
        reference: SecretReference,
        secret: bytes,
        version: str,
        ttl_seconds: float,
    ) -> None:
        self.adapter_id = adapter_id
        self.reference = reference
        self.version = version
        self.issued_at_monotonic = time.monotonic()
        self.expires_at_monotonic = self.issued_at_monotonic + ttl_seconds
        self._secret = bytearray(secret)
        self._closed = False

    def __enter__(self) -> "SecretLease":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"SecretLease(adapter_id={self.adapter_id!r}, version={self.version!r}, "
            f"closed={self._closed})"
        )

    def bearer_value(self) -> str:
        if self._closed or time.monotonic() > self.expires_at_monotonic:
            self.close()
            raise SecretLeaseError("secret lease is closed or expired")
        return bytes(self._secret).decode("utf-8")

    def public(self) -> dict[str, Any]:
        return {
            "adapter_id": self.adapter_id,
            "secret_reference": self.reference.env_name,
            "version": self.version,
            "ttl_seconds": round(
                max(0.0, self.expires_at_monotonic - self.issued_at_monotonic), 3
            ),
            "closed": self._closed,
            "value_exposed": False,
        }

    def close(self) -> None:
        if self._closed:
            return
        for index in range(len(self._secret)):
            self._secret[index] = 0
        self._closed = True


class SecretBroker:
    """The only supported reader of Adapter secret environment values."""

    def __init__(self, *, ttl_seconds: float = 30.0) -> None:
        if not 0.1 <= float(ttl_seconds) <= 300.0:
            raise SecretLeaseError("secret lease TTL is invalid")
        self.ttl_seconds = float(ttl_seconds)
        self._pepper = secrets.token_bytes(32)
        self._last_versions: dict[str, str] = {}
        self._leases_issued = 0
        self._rotations_observed = 0
        self._lock = threading.RLock()

    def lease(self, *, adapter_id: str, reference: SecretReference) -> SecretLease:
        raw = os.environ.get(reference.env_name)
        if raw is None or not raw:
            raise SecretLeaseError("adapter secret is unavailable")
        encoded = raw.encode("utf-8")
        if not 8 <= len(encoded) <= MAX_SECRET_BYTES or b"\x00" in encoded:
            raise SecretLeaseError("adapter secret length or encoding is invalid")
        explicit_version = (
            os.environ.get(reference.version_env)
            if reference.version_env is not None
            else None
        )
        if explicit_version and _SAFE_VERSION_RE.fullmatch(explicit_version):
            version = explicit_version
        else:
            version = "opaque-" + hmac.new(
                self._pepper, encoded, hashlib.sha256
            ).hexdigest()[:16]
        with self._lock:
            prior = self._last_versions.get(reference.env_name)
            if prior is not None and prior != version:
                self._rotations_observed += 1
            self._last_versions[reference.env_name] = version
            self._leases_issued += 1
        return SecretLease(
            adapter_id=adapter_id,
            reference=reference,
            secret=encoded,
            version=version,
            ttl_seconds=self.ttl_seconds,
        )

    def public(self) -> dict[str, Any]:
        with self._lock:
            return {
                "leases_issued": self._leases_issued,
                "rotations_observed": self._rotations_observed,
                "active_secret_values_exposed": 0,
                "persistence_enabled": False,
                "schema_version": SECURITY_SCHEMA_VERSION,
            }


@dataclass(frozen=True, slots=True)
class AdapterPluginManifest:
    plugin_id: str
    adapter_revision: str
    allowed_deployments: tuple[str, ...]
    network_capability: bool
    secret_capability: bool
    tool_capabilities: tuple[str, ...] = ()
    side_effect_mode: str = "provider_idempotency"
    redirects_allowed: int = 0
    max_response_bytes: int = MAX_RESPONSE_BYTES
    schema_version: str = SECURITY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,63}", self.plugin_id):
            raise AdapterAdmissionError("adapter plugin id is invalid")
        if not self.adapter_revision or not self.allowed_deployments:
            raise AdapterAdmissionError("adapter manifest identity is incomplete")
        if any(item not in {"local", "remote"} for item in self.allowed_deployments):
            raise AdapterAdmissionError("adapter deployment capability is invalid")
        if self.redirects_allowed != 0:
            raise AdapterAdmissionError("redirect-capable adapters are forbidden")
        if not 1_024 <= self.max_response_bytes <= MAX_RESPONSE_BYTES:
            raise AdapterAdmissionError("adapter response bound is invalid")
        if self.side_effect_mode not in {
            "provider_idempotency",
            "explicit_compensation",
            "none",
        }:
            raise AdapterAdmissionError("adapter side-effect mode is invalid")

    def public(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "allowed_deployments": list(self.allowed_deployments),
            "tool_capabilities": list(self.tool_capabilities),
        }


class AdapterPluginRegistry:
    def __init__(self, manifests: Iterable[AdapterPluginManifest]) -> None:
        self._manifests: dict[str, AdapterPluginManifest] = {}
        for manifest in manifests:
            if manifest.plugin_id in self._manifests:
                raise AdapterAdmissionError("duplicate adapter plugin id")
            self._manifests[manifest.plugin_id] = manifest

    def require(
        self,
        plugin_id: str,
        *,
        adapter_revision: str,
        deployment: str,
        secret_required: bool,
    ) -> AdapterPluginManifest:
        manifest = self._manifests.get(plugin_id)
        if manifest is None:
            raise AdapterAdmissionError("unregistered adapter plugin")
        if manifest.adapter_revision != adapter_revision:
            raise AdapterAdmissionError("adapter revision is not registered")
        if deployment not in manifest.allowed_deployments:
            raise AdapterAdmissionError("adapter deployment is outside manifest scope")
        if secret_required and not manifest.secret_capability:
            raise AdapterAdmissionError("adapter manifest does not allow secrets")
        return manifest

    def public(self) -> list[dict[str, Any]]:
        return [self._manifests[key].public() for key in sorted(self._manifests)]


DEFAULT_ADAPTER_REGISTRY = AdapterPluginRegistry(
    (
        AdapterPluginManifest(
            plugin_id="openai-compatible-http",
            adapter_revision="openai-compatible-http-v1",
            allowed_deployments=("remote",),
            network_capability=True,
            secret_capability=True,
        ),
        AdapterPluginManifest(
            plugin_id="local-http",
            adapter_revision="local-http-v1",
            allowed_deployments=("local",),
            network_capability=True,
            secret_capability=False,
            side_effect_mode="provider_idempotency",
        ),
    )
)


def validate_endpoint(endpoint: str, *, deployment: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AdapterAdmissionError("adapter endpoint is invalid")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise AdapterAdmissionError("adapter endpoint must not contain credentials, query or fragment")
    hostname = parsed.hostname.rstrip(".").lower()
    if not _HOST_RE.fullmatch(hostname) and ":" not in hostname:
        raise AdapterAdmissionError("adapter endpoint hostname is invalid")
    local_hosts = {"127.0.0.1", "localhost", "::1"}
    if deployment == "local":
        if hostname not in local_hosts:
            raise AdapterAdmissionError("local adapter must use an explicit loopback endpoint")
    elif deployment == "remote":
        if parsed.scheme != "https" or hostname in local_hosts:
            raise AdapterAdmissionError("remote adapter requires a non-loopback HTTPS endpoint")
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise AdapterAdmissionError("remote adapter cannot target a private or reserved address")
    else:
        raise AdapterAdmissionError("adapter deployment is invalid")
    return hostname


def validate_remote_resolution(
    endpoint: str,
    *,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
) -> tuple[str, ...]:
    parsed = urlparse(endpoint)
    hostname = validate_endpoint(endpoint, deployment="remote")
    try:
        rows = resolver(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as error:
        raise AdapterAdmissionError("remote adapter hostname resolution failed") from error
    addresses = tuple(sorted({str(row[4][0]).split("%", 1)[0] for row in rows}))
    if not addresses:
        raise AdapterAdmissionError("remote adapter hostname has no address")
    for raw in addresses:
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as error:
            raise AdapterAdmissionError("remote adapter resolution returned an invalid address") from error
        if not address.is_global:
            raise AdapterAdmissionError("remote adapter resolved to a private or reserved address")
    return addresses


def public_security_policy(
    *, runtime_counts: Mapping[str, int] | None = None
) -> dict[str, Any]:
    return {
        "schema_version": SECURITY_SCHEMA_VERSION,
        "policy_revision": SECURITY_POLICY_REVISION,
        "default_project_id": DEFAULT_PROJECT_ID,
        "default_data_residency": DEFAULT_DATA_RESIDENCY,
        "adapter_registry": DEFAULT_ADAPTER_REGISTRY.public(),
        "external_network_default_enabled": False,
        "paid_calls_default_enabled": False,
        "unknown_plugins_fail_closed": True,
        "remote_https_required": True,
        "redirects_allowed": 0,
        "private_remote_targets_allowed": False,
        "max_response_bytes": MAX_RESPONSE_BYTES,
        "secret_values_persisted": False,
        "secret_values_publicly_exposed": False,
        "secret_rotation_observable_without_value": True,
        "shared_structural_redaction": True,
        "project_scope_persisted": True,
        "cross_project_not_found_semantics": True,
        "logical_project_revocation_supported": True,
        "physical_project_erasure_supported": False,
        "prompt_untrusted_fields": ["objective", "assumptions"],
        "prompt_override_requires_separate_approval": True,
        "http_adapter_tool_capabilities": [],
        "provider_idempotency_required": True,
        "ambiguous_side_effect_auto_replay": False,
        "runtime_counts": dict(runtime_counts or {}),
    }


__all__ = [
    "AdapterAdmissionError",
    "AdapterPluginManifest",
    "AdapterPluginRegistry",
    "DEFAULT_ADAPTER_REGISTRY",
    "DEFAULT_DATA_RESIDENCY",
    "DEFAULT_PROJECT_ID",
    "MAX_RESPONSE_BYTES",
    "PromptInjectionError",
    "PromptSecurityAssessment",
    "ProjectScopeError",
    "SECURITY_POLICY_REVISION",
    "SECURITY_SCHEMA_VERSION",
    "SecretBroker",
    "SecretLease",
    "SecretLeaseError",
    "SecretReference",
    "SecurityBoundaryError",
    "assess_prompt_security",
    "contains_secret_material",
    "normalize_data_residency",
    "normalize_project_id",
    "public_security_policy",
    "redact_structure",
    "redact_text",
    "require_prompt_admission",
    "validate_endpoint",
    "validate_remote_resolution",
]
