"""Machine-verifiable v0.6.6 lab release readiness contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from .observability import (
    ObservabilityError,
    TraceRecorder,
    TraceSpanV1,
    public_observability_policy,
    sanitize_attributes,
)
from .runtime_backend import (
    RuntimeCapabilitiesV1,
    RUNTIME_OPERATION_TYPES,
    public_runtime_backend_policy,
)


RELEASE_SCHEMA_VERSION = "v0.6.6"
EXPECTED_RUNTIME_SCHEMA = "v6.5.0"
EXPECTED_MIGRATIONS = 35


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def audit_event_replay(store: Any) -> dict[str, Any]:
    """Compare every durable run projection with an EventLog-only replay."""

    run_ids = store.projection_ids()
    mismatches: list[dict[str, str]] = []
    for run_id in run_ids:
        try:
            projection = store.get_projection(run_id)
            replayed = store.replay(run_id)
            if projection is None or replayed is None:
                mismatches.append({"run_id": run_id, "reason": "missing_state"})
            elif _sha256(projection) != _sha256(replayed):
                mismatches.append({"run_id": run_id, "reason": "state_mismatch"})
        except Exception as exc:  # the audit reports, never hides, replay errors
            mismatches.append(
                {"run_id": run_id, "reason": f"replay_error:{type(exc).__name__}"}
            )
    return {
        "healthy": not mismatches,
        "complete": True,
        "runs_checked": len(run_ids),
        "mismatches": mismatches[:100],
    }


def audit_observability_safety() -> dict[str, Any]:
    """Exercise the security invariants claimed by the WP05 release gate."""

    sentinels = {
        "auth": "Basic cWEtdXNlcjpxYS1wYXNzd29yZA==",
        "pass": "qa-pass-value",
        "pwd": "qa-password-value",
        "jwt": "eyJhbGciOiJIUzI1NiJ9.cWEtcGF5bG9hZA.cWEtc2lnbmF0dXJl",
        "session_id": "qa-session-value",
        "access_key": "AKIAIOSFODNN7EXAMPLE",
        "database_url": "postgresql://qa-user:qa-db-password@db.internal/app",
        "dsn": "mysql://root:qa-mysql-password@db.internal/app",
        "oauth": "qa-oauth-credential",
        "connection_string": "Server=db;Password=qa-connection-password",
        "provider_endpoint": "redis://:qa-redis-password@cache.internal/0",
        "max_tokens": "qa-token-shaped-secret",
        "api.key": "qa-dotted-api-key",
        "access:key": "qa-colon-access-key",
        "database.url": "qa-dotted-database-url",
        "connection:string": "qa-colon-connection-string",
        "api..key": "qa-repeated-api-key",
        "connection::string": "qa-repeated-connection-string",
    }
    redacted = sanitize_attributes({
        "nested": {"items": [sentinels]},
        "input_tokens": 7,
    })
    encoded = _canonical_json(redacted)
    leaked_values = [
        key for key, value in sentinels.items() if value in encoded
    ]
    digest_retained = "sha256" in encoded.lower()
    numeric_token_metric_preserved = redacted.get("input_tokens") == 7

    sanitizer_rejected_nonfinite = False
    try:
        sanitize_attributes({"latency_ms": float("nan")})
    except ObservabilityError:
        sanitizer_rejected_nonfinite = True
    serializer_rejected_nonfinite = False
    try:
        _canonical_json({"latency_ms": float("inf")})
    except ValueError:
        serializer_rejected_nonfinite = True
    finite_numbers_rejected = (
        sanitizer_rejected_nonfinite and serializer_rejected_nonfinite
    )

    event_secret = "qa-event-extra-secret"
    secret_span_name = "sk-qa-span-secret-12345678"
    span = TraceSpanV1(
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id=None,
        name="costweave.release.audit",
        stage="validation",
        start_time="2026-08-14T00:00:00Z",
        end_time="2026-08-14T00:00:01Z",
        attributes={"nested": {"safe_count": 1}},
        events=(
            {
                "name": "release.audit",
                "timestamp": "2026-08-14T00:00:00Z",
                "attributes": {},
                "secret": event_secret,
            },
        ),
    )
    event_extra_leaked = event_secret in _canonical_json(span.to_dict())
    secret_name_span = TraceSpanV1(
        trace_id="c" * 32,
        span_id="d" * 16,
        parent_span_id=None,
        name=secret_span_name,
        stage="validation",
        start_time="2026-08-14T00:00:00Z",
    )
    span_name_leaked = secret_span_name in _canonical_json(
        secret_name_span.to_dict()
    )
    secret_event_name = "Basic cWEtZXZlbnQ6cWEtcGFzc3dvcmQ="
    name_recorder = TraceRecorder(trace_id="e" * 32)
    secret_event_span = name_recorder.record_event(secret_event_name)
    event_name_export = _canonical_json(name_recorder.export()).lower()
    event_name_leaked = (
        secret_event_span is None
        or secret_event_span.name != "costweave.redacted.event"
        or secret_event_name.split(" ", 1)[1].lower() in event_name_export
    )
    deeply_immutable = True
    try:
        span.attributes["nested"]["safe_count"] = 2
    except TypeError:
        pass
    else:
        deeply_immutable = False
    exported = span.to_dict()
    exported["attributes"]["nested"]["safe_count"] = 3
    export_isolated = (
        span.to_dict()["attributes"]["nested"]["safe_count"] == 1
    )
    deeply_immutable = deeply_immutable and export_isolated

    return {
        "healthy": (
            not leaked_values
            and not digest_retained
            and numeric_token_metric_preserved
            and finite_numbers_rejected
            and not event_extra_leaked
            and not span_name_leaked
            and not event_name_leaked
            and deeply_immutable
        ),
        "leaked_values": leaked_values,
        "digest_retained": digest_retained,
        "numeric_token_metric_preserved": numeric_token_metric_preserved,
        "finite_numbers_rejected": finite_numbers_rejected,
        "event_extra_leaked": event_extra_leaked,
        "span_name_leaked": span_name_leaked,
        "event_name_leaked": event_name_leaked,
        "deeply_immutable": deeply_immutable,
        "export_isolated": export_isolated,
    }


def audit_runtime_backend_contract() -> dict[str, Any]:
    """Verify the local-first Runtime Backend cannot silently enable calls."""

    capabilities = RuntimeCapabilitiesV1().to_dict()
    policy = public_runtime_backend_policy()
    expected_operations = sorted({"submit", "cancel", "checkpoint", "resume"})
    observed_operations = sorted(policy.get("supported_operations", []))
    healthy = (
        capabilities.get("backend_type") == "local"
        and capabilities.get("persistent") is True
        and all(capabilities.get(operation) is True for operation in expected_operations)
        and capabilities.get("external_calls_enabled") is False
        and policy.get("local_first") is True
        and policy.get("persistent_runtime_required") is True
        and policy.get("external_calls_enabled_by_default") is False
        and policy.get("third_party_runtime_mode") == "adapter-only"
        and observed_operations == sorted(RUNTIME_OPERATION_TYPES)
        and policy.get("events_are_project_scoped") is True
        and policy.get("checkpoint_is_idempotent") is True
        and policy.get("resume_is_idempotent") is True
    )
    return {
        "healthy": healthy,
        "capabilities": capabilities,
        "policy": policy,
        "expected_operations": expected_operations,
        "observed_operations": observed_operations,
    }


def public_release_policy() -> dict[str, Any]:
    return {
        "schema_version": RELEASE_SCHEMA_VERSION,
        "release_series": "v0.6-lab",
        "engineering_stable_scope": "offline single-process control plane",
        "real_provider_calls_default_enabled": False,
        "paid_calls_default_enabled": False,
        "commercial_claims_allowed": False,
        "production_ready": False,
        "required_release_artifacts": [
            "wheel",
            "source_archive",
            "runtime_archive",
            "sha256sums",
            "benchmark_report",
            "model_card",
            "policy_card",
            "data_card",
            "security_report",
            "acceptance_report",
            "installation_and_reproduction_guide",
        ],
        "verified_scope": [
            "deterministic offline orchestration contracts",
            "append-only event and projection integrity",
            "EventLog-only projection replay",
            "logical backup verification and restore",
            "default-off external and paid adapters",
            "default-off local endpoint and capacity admission",
            "bounded OpenTelemetry-compatible local tracing",
            "trace ID propagation through durable runtime events",
            "local-first Runtime Backend submit/cancel/checkpoint/resume/events contract",
        ],
        "unverified_claims": [
            "real provider answer quality",
            "real cost reduction percentage",
            "real end-to-end latency improvement",
            "real Ollama/vLLM queue or GPU capacity",
            "production availability and distributed recovery",
            "external OpenTelemetry collector delivery and production telemetry cost",
            "commercial superiority or world-leading performance",
        ],
    }


def build_release_status(
    kernel: Any,
    *,
    adapter_policy: Mapping[str, Any] | None = None,
    litellm_policy: Mapping[str, Any] | None = None,
    local_policy: Mapping[str, Any] | None = None,
    telemetry_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate only facts that the running installation can verify itself."""

    runtime = kernel.health()
    integrity = kernel.resilience_store.integrity_audit()
    replay = audit_event_replay(kernel.store)
    observability_audit = audit_observability_safety()
    runtime_backend_audit = audit_runtime_backend_contract()
    adapter = dict(adapter_policy or {})
    experimental_enabled = bool(adapter.get("experimental_calls_enabled", False))
    paid_enabled = bool(adapter.get("paid_calls_enabled", False))
    litellm = dict(litellm_policy or {})
    litellm_gateway_enabled = bool(litellm.get("gateway_enabled", False))
    litellm_paid_enabled = bool(litellm.get("paid_calls_enabled", False))
    local = dict(local_policy or {})
    local_network_enabled = bool(local.get("network_enabled", False))
    local_capacity_enabled = bool(local.get("capacity_admission_enabled", False))
    telemetry = dict(telemetry_policy or public_observability_policy())
    telemetry_external_enabled = bool(telemetry.get("external_export_enabled", False))
    telemetry_prompt_enabled = bool(telemetry.get("prompt_capture_enabled", False))
    telemetry_secret_enabled = bool(telemetry.get("secret_capture_enabled", False))
    gates = [
        {
            "gate_id": "runtime-schema",
            "passed": runtime.get("runtime_schema") == EXPECTED_RUNTIME_SCHEMA,
            "observed": runtime.get("runtime_schema"),
            "expected": EXPECTED_RUNTIME_SCHEMA,
        },
        {
            "gate_id": "migration-set",
            "passed": int(runtime.get("migrations", 0)) == EXPECTED_MIGRATIONS,
            "observed": int(runtime.get("migrations", 0)),
            "expected": EXPECTED_MIGRATIONS,
        },
        {
            "gate_id": "sqlite-wal",
            "passed": runtime.get("journal_mode") == "wal",
            "observed": runtime.get("journal_mode"),
            "expected": "wal",
        },
        {
            "gate_id": "event-projection-integrity",
            "passed": bool(integrity.get("healthy")),
            "observed": len(integrity.get("errors") or []),
            "expected": 0,
        },
        {
            "gate_id": "eventlog-replay-parity",
            "passed": bool(replay.get("healthy") and replay.get("complete")),
            "observed": len(replay.get("mismatches") or []),
            "expected": 0,
        },
        {
            "gate_id": "external-adapters-default-off",
            "passed": not experimental_enabled,
            "observed": experimental_enabled,
            "expected": False,
        },
        {
            "gate_id": "paid-calls-default-off",
            "passed": not paid_enabled,
            "observed": paid_enabled,
            "expected": False,
        },
        {
            "gate_id": "litellm-gateway-default-off",
            "passed": not litellm_gateway_enabled,
            "observed": litellm_gateway_enabled,
            "expected": False,
        },
        {
            "gate_id": "litellm-paid-calls-default-off",
            "passed": not litellm_paid_enabled,
            "observed": litellm_paid_enabled,
            "expected": False,
        },
        {
            "gate_id": "local-endpoint-network-default-off",
            "passed": not local_network_enabled,
            "observed": local_network_enabled,
            "expected": False,
        },
        {
            "gate_id": "local-capacity-admission-default-off",
            "passed": not local_capacity_enabled,
            "observed": local_capacity_enabled,
            "expected": False,
        },
        {
            "gate_id": "otel-external-export-default-off",
            "passed": not telemetry_external_enabled,
            "observed": telemetry_external_enabled,
            "expected": False,
        },
        {
            "gate_id": "runtime-backend-contract-self-test",
            "passed": runtime_backend_audit["healthy"],
            "observed": {
                "backend_type": runtime_backend_audit["capabilities"].get("backend_type"),
                "persistent": runtime_backend_audit["capabilities"].get("persistent"),
                "external_calls_enabled": runtime_backend_audit["capabilities"].get("external_calls_enabled"),
                "operations": runtime_backend_audit["observed_operations"],
                "third_party_runtime_mode": runtime_backend_audit["policy"].get("third_party_runtime_mode"),
            },
            "expected": {
                "backend_type": "local",
                "persistent": True,
                "external_calls_enabled": False,
                "operations": sorted(RUNTIME_OPERATION_TYPES),
                "third_party_runtime_mode": "adapter-only",
            },
        },
        {
            "gate_id": "otel-prompt-secret-capture-disabled",
            "passed": not telemetry_prompt_enabled and not telemetry_secret_enabled,
            "observed": {
                "prompt_capture_enabled": telemetry_prompt_enabled,
                "secret_capture_enabled": telemetry_secret_enabled,
            },
            "expected": {
                "prompt_capture_enabled": False,
                "secret_capture_enabled": False,
            },
        },
        {
            "gate_id": "otel-redaction-self-test",
            "passed": not observability_audit["leaked_values"]
            and not observability_audit["event_extra_leaked"]
            and not observability_audit["span_name_leaked"]
            and not observability_audit["event_name_leaked"]
            and not observability_audit["digest_retained"]
            and observability_audit["numeric_token_metric_preserved"],
            "observed": {
                "leaked_values": observability_audit["leaked_values"],
                "event_extra_leaked": observability_audit[
                    "event_extra_leaked"
                ],
                "span_name_leaked": observability_audit[
                    "span_name_leaked"
                ],
                "event_name_leaked": observability_audit[
                    "event_name_leaked"
                ],
                "digest_retained": observability_audit["digest_retained"],
                "numeric_token_metric_preserved": observability_audit[
                    "numeric_token_metric_preserved"
                ],
            },
            "expected": {
                "leaked_values": [],
                "event_extra_leaked": False,
                "span_name_leaked": False,
                "event_name_leaked": False,
                "digest_retained": False,
                "numeric_token_metric_preserved": True,
            },
        },
        {
            "gate_id": "otel-strict-json-self-test",
            "passed": observability_audit["finite_numbers_rejected"],
            "observed": observability_audit["finite_numbers_rejected"],
            "expected": True,
        },
        {
            "gate_id": "otel-deep-immutability-self-test",
            "passed": observability_audit["deeply_immutable"],
            "observed": observability_audit["deeply_immutable"],
            "expected": True,
        },
    ]
    engineering_ready = all(bool(item["passed"]) for item in gates)
    manifest = {
        **public_release_policy(),
        "release_status": (
            "stable_engineering" if engineering_ready else "blocked"
        ),
        "engineering_ready": engineering_ready,
        "runtime": {
            "schema_version": runtime.get("runtime_schema"),
            "migrations": runtime.get("migrations"),
            "journal_mode": runtime.get("journal_mode"),
            "persistent": bool(runtime.get("persistent")),
        },
        "gates": gates,
        "integrity": integrity,
        "replay_audit": replay,
        "observability": telemetry,
        "observability_audit": observability_audit,
        "runtime_backend_audit": runtime_backend_audit,
    }
    manifest["manifest_hash"] = _sha256(manifest)
    return manifest


__all__ = [
    "EXPECTED_MIGRATIONS",
    "EXPECTED_RUNTIME_SCHEMA",
    "RELEASE_SCHEMA_VERSION",
    "audit_event_replay",
    "audit_observability_safety",
    "audit_runtime_backend_contract",
    "build_release_status",
    "public_release_policy",
]
