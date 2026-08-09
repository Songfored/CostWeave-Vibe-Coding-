"""Machine-verifiable v0.6.1 lab release readiness contracts."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


RELEASE_SCHEMA_VERSION = "v0.6.1"
EXPECTED_RUNTIME_SCHEMA = "v6.1.0"
EXPECTED_MIGRATIONS = 31


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
        ],
        "unverified_claims": [
            "real provider answer quality",
            "real cost reduction percentage",
            "real end-to-end latency improvement",
            "production availability and distributed recovery",
            "commercial superiority or world-leading performance",
        ],
    }


def build_release_status(
    kernel: Any,
    *,
    adapter_policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Evaluate only facts that the running installation can verify itself."""

    runtime = kernel.health()
    integrity = kernel.resilience_store.integrity_audit()
    replay = audit_event_replay(kernel.store)
    adapter = dict(adapter_policy or {})
    experimental_enabled = bool(adapter.get("experimental_calls_enabled", False))
    paid_enabled = bool(adapter.get("paid_calls_enabled", False))
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
    }
    manifest["manifest_hash"] = _sha256(manifest)
    return manifest


__all__ = [
    "EXPECTED_MIGRATIONS",
    "EXPECTED_RUNTIME_SCHEMA",
    "RELEASE_SCHEMA_VERSION",
    "audit_event_replay",
    "build_release_status",
    "public_release_policy",
]
