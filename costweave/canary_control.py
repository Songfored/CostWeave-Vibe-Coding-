"""Bounded runtime canary and automatic rollback contracts for v0.5.7.4.

The canary control plane selects a *versioned routing policy snapshot* for a
new run.  It does not grant provider credentials or expand tool permissions.
Assignments are deterministic, high/elevated-risk work is never explored,
and a running job keeps the snapshot captured at admission even after a
rollback changes the policy selected for later jobs.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from .evaluation_protocol import canonical_json


CANARY_SCHEMA_VERSION = "v0.5.7.4"
CANARY_POLICY_REVISION = "bounded-canary-rollback-v1"
CANARY_ENABLED_ENV = "COSTWEAVE_CANARY_ENABLED"

CANARY_STAGES = (5, 25, 50, 100)
NEXT_STAGE = {5: 25, 25: 50, 50: 100}
MINIMUM_CANDIDATE_CASES = {5: 50, 25: 100, 50: 250, 100: 1_000}
SUCCESS_NON_INFERIORITY_LOWER_PP = -1.0
MAXIMUM_P95_DEGRADATION_RATIO = 0.10
EXPLORABLE_RISK_LEVELS = frozenset({"normal", "low"})


class CanaryControlError(ValueError):
    """Raised when a canary contract would weaken a safety boundary."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _bounded(value: Any, name: str, *, limit: int = 240) -> str:
    text = str(value).strip()
    if not text or len(text) > limit or "\n" in text or "\r" in text:
        raise CanaryControlError(f"{name} must be a bounded identifier")
    lowered = text.lower()
    if any(marker in lowered for marker in ("bearer ", "api_key=", "password=", "secret=")):
        raise CanaryControlError(f"{name} appears to contain a secret")
    return text


def _anonymous(value: Any, prefix: str, name: str) -> str:
    text = str(value)
    suffix = text.removeprefix(prefix)
    if (
        not text.startswith(prefix)
        or len(suffix) != 32
        or any(char not in "0123456789abcdef" for char in suffix.lower())
    ):
        raise CanaryControlError(f"{name} must be an anonymous identifier")
    return text


def _timestamp(value: Any, name: str) -> str:
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CanaryControlError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise CanaryControlError(f"{name} must include a timezone")
    return text


def _finite(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    if isinstance(value, bool):
        raise CanaryControlError(f"{name} must be numeric")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise CanaryControlError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise CanaryControlError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise CanaryControlError(f"{name} is below its minimum")
    if maximum is not None and number > maximum:
        raise CanaryControlError(f"{name} exceeds its maximum")
    return number


def _count(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CanaryControlError(f"{name} must be a non-negative integer")
    return value


def _unique(values: Sequence[str], name: str) -> tuple[str, ...]:
    result = tuple(_bounded(item, name) for item in values)
    if len(result) != len(set(result)):
        raise CanaryControlError(f"{name} values must be unique")
    return result


def _strict_bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise CanaryControlError(f"{name} must be boolean")
    return value


def canary_runtime_enabled() -> bool:
    return os.environ.get(CANARY_ENABLED_ENV, "0").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True, slots=True)
class CanaryDeployment:
    """Explicit authorization to expose low-risk new runs to a candidate."""

    deployment_id: str
    candidate_policy_id: str
    candidate_policy_version: str
    stable_policy_id: str
    stable_policy_version: str
    candidate_manifest_hash: str
    stable_manifest_hash: str
    canary_gate_report_id: str
    activation_approval_id: str
    allowed_task_families: tuple[str, ...]
    prohibited_task_families: tuple[str, ...]
    explanation: str
    rollback_target: str
    started_at: str
    initial_stage_pct: int = 5
    automatic_rollback: bool = True
    high_risk_exploration_rate: float = 0.0
    provider_credentials_granted: bool = False
    tool_permissions_expanded: bool = False
    schema_version: str = CANARY_SCHEMA_VERSION
    policy_revision: str = CANARY_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.schema_version != CANARY_SCHEMA_VERSION or self.policy_revision != CANARY_POLICY_REVISION:
            raise CanaryControlError("canary deployment schema is invalid")
        _anonymous(self.deployment_id, "canarydep_", "deployment_id")
        _anonymous(self.candidate_policy_id, "policyreg_", "candidate_policy_id")
        _anonymous(self.stable_policy_id, "policyreg_", "stable_policy_id")
        if self.candidate_policy_id == self.stable_policy_id:
            raise CanaryControlError("candidate and stable policies must differ")
        for name in (
            "candidate_policy_version",
            "stable_policy_version",
            "candidate_manifest_hash",
            "stable_manifest_hash",
            "canary_gate_report_id",
            "activation_approval_id",
            "explanation",
            "rollback_target",
        ):
            _bounded(getattr(self, name), name)
        if len(self.candidate_manifest_hash) != 64 or len(self.stable_manifest_hash) != 64:
            raise CanaryControlError("manifest hashes must be sha256 digests")
        if any(
            char not in "0123456789abcdef"
            for char in (self.candidate_manifest_hash + self.stable_manifest_hash).lower()
        ):
            raise CanaryControlError("manifest hashes must be sha256 digests")
        if not str(self.canary_gate_report_id).startswith("polgate_"):
            raise CanaryControlError("canary gate report identity is invalid")
        allowed = _unique(self.allowed_task_families, "allowed_task_families")
        prohibited = _unique(self.prohibited_task_families, "prohibited_task_families")
        if not allowed or set(allowed) & set(prohibited):
            raise CanaryControlError("canary task-family scope is invalid")
        if self.initial_stage_pct != 5:
            raise CanaryControlError("a canary deployment must begin at 5 percent")
        _strict_bool(self.automatic_rollback, "automatic_rollback")
        _strict_bool(self.provider_credentials_granted, "provider_credentials_granted")
        _strict_bool(self.tool_permissions_expanded, "tool_permissions_expanded")
        if not self.automatic_rollback:
            raise CanaryControlError("automatic rollback cannot be disabled")
        if self.provider_credentials_granted or self.tool_permissions_expanded:
            raise CanaryControlError("canary activation cannot expand credentials or permissions")
        if _finite(self.high_risk_exploration_rate, "high_risk_exploration_rate", minimum=0, maximum=1) != 0:
            raise CanaryControlError("high-risk canary exploration must be zero")
        _timestamp(self.started_at, "started_at")
        expected_target = f"{self.stable_policy_id}@{self.stable_policy_version}"
        if self.rollback_target != expected_target:
            raise CanaryControlError("rollback target does not identify the stable policy")
        identity = self.identity()
        expected_id = "canarydep_" + _digest(identity)[:32]
        if self.deployment_id != expected_id:
            raise CanaryControlError("canary deployment identity does not match its content")

    def identity(self) -> dict[str, Any]:
        return {
            "candidate_policy_id": self.candidate_policy_id,
            "candidate_policy_version": self.candidate_policy_version,
            "stable_policy_id": self.stable_policy_id,
            "stable_policy_version": self.stable_policy_version,
            "candidate_manifest_hash": self.candidate_manifest_hash,
            "stable_manifest_hash": self.stable_manifest_hash,
            "canary_gate_report_id": self.canary_gate_report_id,
            "activation_approval_id": self.activation_approval_id,
            "allowed_task_families": list(self.allowed_task_families),
            "prohibited_task_families": list(self.prohibited_task_families),
            "explanation": self.explanation,
            "rollback_target": self.rollback_target,
            "started_at": self.started_at,
            "initial_stage_pct": self.initial_stage_pct,
            "automatic_rollback": self.automatic_rollback,
            "high_risk_exploration_rate": self.high_risk_exploration_rate,
            "provider_credentials_granted": self.provider_credentials_granted,
            "tool_permissions_expanded": self.tool_permissions_expanded,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"deployment_id": self.deployment_id, **self.identity()}

    @classmethod
    def create(cls, **values: Any) -> "CanaryDeployment":
        values = dict(values)
        values.setdefault("initial_stage_pct", 5)
        values.setdefault("automatic_rollback", True)
        values.setdefault("high_risk_exploration_rate", 0.0)
        values.setdefault("provider_credentials_granted", False)
        values.setdefault("tool_permissions_expanded", False)
        values.setdefault("schema_version", CANARY_SCHEMA_VERSION)
        values.setdefault("policy_revision", CANARY_POLICY_REVISION)
        values.setdefault(
            "rollback_target",
            f"{values['stable_policy_id']}@{values['stable_policy_version']}",
        )
        identity = {key: value for key, value in values.items() if key != "deployment_id"}
        identity["allowed_task_families"] = list(identity["allowed_task_families"])
        identity["prohibited_task_families"] = list(identity["prohibited_task_families"])
        values["deployment_id"] = "canarydep_" + _digest(identity)[:32]
        values["allowed_task_families"] = tuple(values["allowed_task_families"])
        values["prohibited_task_families"] = tuple(values["prohibited_task_families"])
        return cls(**values)


@dataclass(frozen=True, slots=True)
class CanaryAssignment:
    assignment_id: str
    deployment_id: str
    run_id: str
    stage_pct: int
    bucket: int
    risk_level: str
    task_families: tuple[str, ...]
    eligible_for_exploration: bool
    cohort: str
    selected_policy_id: str
    selected_policy_version: str
    stable_policy_id: str
    stable_policy_version: str
    reasons: tuple[str, ...]
    assigned_at: str
    policy_snapshot_hash: str
    runtime_policy_changed: bool
    provider_call_executed: bool = False
    schema_version: str = CANARY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CANARY_SCHEMA_VERSION:
            raise CanaryControlError("canary assignment schema is invalid")
        _anonymous(self.assignment_id, "canaryasn_", "assignment_id")
        _anonymous(self.deployment_id, "canarydep_", "deployment_id")
        _bounded(self.run_id, "run_id")
        if self.stage_pct not in CANARY_STAGES or not 0 <= self.bucket < 10_000:
            raise CanaryControlError("canary assignment stage or bucket is invalid")
        _bounded(self.risk_level, "risk_level")
        _unique(self.task_families, "task_families")
        if self.cohort not in {"candidate", "control", "stable-only"}:
            raise CanaryControlError("canary assignment cohort is invalid")
        for name in (
            "selected_policy_id",
            "selected_policy_version",
            "stable_policy_id",
            "stable_policy_version",
            "policy_snapshot_hash",
        ):
            _bounded(getattr(self, name), name)
        if len(self.policy_snapshot_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.policy_snapshot_hash.lower()
        ):
            raise CanaryControlError("policy_snapshot_hash must be a sha256 digest")
        _unique(self.reasons, "reasons")
        _timestamp(self.assigned_at, "assigned_at")
        _strict_bool(self.eligible_for_exploration, "eligible_for_exploration")
        _strict_bool(self.runtime_policy_changed, "runtime_policy_changed")
        _strict_bool(self.provider_call_executed, "provider_call_executed")
        if self.provider_call_executed:
            raise CanaryControlError("assignment cannot claim that a provider call executed")
        if self.risk_level not in EXPLORABLE_RISK_LEVELS and self.cohort == "candidate":
            raise CanaryControlError("elevated or high-risk work cannot enter the candidate cohort")
        if self.runtime_policy_changed != (self.cohort == "candidate"):
            raise CanaryControlError("runtime policy change flag is inconsistent")
        snapshot = {
            "deployment_id": self.deployment_id,
            "run_id": self.run_id,
            "stage_pct": self.stage_pct,
            "cohort": self.cohort,
            "selected_policy_id": self.selected_policy_id,
            "selected_policy_version": self.selected_policy_version,
            "stable_policy_id": self.stable_policy_id,
            "stable_policy_version": self.stable_policy_version,
            "assigned_at": self.assigned_at,
        }
        if self.policy_snapshot_hash != _digest(snapshot):
            raise CanaryControlError("policy snapshot hash does not match assignment content")
        identity = {
            **snapshot,
            "bucket": self.bucket,
            "risk_level": self.risk_level,
            "task_families": list(self.task_families),
            "eligible_for_exploration": self.eligible_for_exploration,
            "reasons": list(self.reasons),
            "policy_snapshot_hash": self.policy_snapshot_hash,
        }
        if self.assignment_id != "canaryasn_" + _digest(identity)[:32]:
            raise CanaryControlError("canary assignment identity does not match its content")

    def to_dict(self) -> dict[str, Any]:
        return {
            "assignment_id": self.assignment_id,
            "deployment_id": self.deployment_id,
            "run_id": self.run_id,
            "stage_pct": self.stage_pct,
            "bucket": self.bucket,
            "risk_level": self.risk_level,
            "task_families": list(self.task_families),
            "eligible_for_exploration": self.eligible_for_exploration,
            "cohort": self.cohort,
            "selected_policy_id": self.selected_policy_id,
            "selected_policy_version": self.selected_policy_version,
            "stable_policy_id": self.stable_policy_id,
            "stable_policy_version": self.stable_policy_version,
            "reasons": list(self.reasons),
            "assigned_at": self.assigned_at,
            "policy_snapshot_hash": self.policy_snapshot_hash,
            "runtime_policy_changed": self.runtime_policy_changed,
            "provider_call_executed": self.provider_call_executed,
            "schema_version": self.schema_version,
        }


def assign_canary_policy(
    deployment: CanaryDeployment,
    *,
    run_id: str,
    stage_pct: int,
    risk_level: str,
    task_families: Sequence[str],
    exposure_enabled: bool = True,
    assigned_at: str | None = None,
) -> CanaryAssignment:
    """Select a candidate/control cohort without mutable randomness."""

    if stage_pct not in CANARY_STAGES:
        raise CanaryControlError("canary stage is invalid")
    run = _bounded(run_id, "run_id")
    risk = _bounded(risk_level, "risk_level").lower()
    families = _unique(tuple(task_families), "task_families")
    _strict_bool(exposure_enabled, "exposure_enabled")
    bucket = int(
        hashlib.sha256(f"{deployment.deployment_id}:{run}".encode("utf-8")).hexdigest()[:8],
        16,
    ) % 10_000
    reasons: list[str] = []
    if not exposure_enabled:
        reasons.append("deployment_rolled_back")
    if risk not in EXPLORABLE_RISK_LEVELS:
        reasons.append("risk_not_explorable")
    if not families:
        reasons.append("task_family_missing")
    if any(item in deployment.prohibited_task_families for item in families):
        reasons.append("prohibited_task_family")
    if any(item not in deployment.allowed_task_families for item in families):
        reasons.append("task_family_out_of_scope")
    eligible = not reasons
    candidate = eligible and bucket < stage_pct * 100
    cohort = "candidate" if candidate else ("control" if eligible else "stable-only")
    selected_id = deployment.candidate_policy_id if candidate else deployment.stable_policy_id
    selected_version = (
        deployment.candidate_policy_version if candidate else deployment.stable_policy_version
    )
    timestamp = assigned_at or datetime.now(timezone.utc).isoformat()
    snapshot = {
        "deployment_id": deployment.deployment_id,
        "run_id": run,
        "stage_pct": stage_pct,
        "cohort": cohort,
        "selected_policy_id": selected_id,
        "selected_policy_version": selected_version,
        "stable_policy_id": deployment.stable_policy_id,
        "stable_policy_version": deployment.stable_policy_version,
        "assigned_at": timestamp,
    }
    snapshot_hash = _digest(snapshot)
    identity = {
        **snapshot,
        "bucket": bucket,
        "risk_level": risk,
        "task_families": list(families),
        "eligible_for_exploration": eligible,
        "reasons": reasons,
        "policy_snapshot_hash": snapshot_hash,
    }
    return CanaryAssignment(
        assignment_id="canaryasn_" + _digest(identity)[:32],
        deployment_id=deployment.deployment_id,
        run_id=run,
        stage_pct=stage_pct,
        bucket=bucket,
        risk_level=risk,
        task_families=families,
        eligible_for_exploration=eligible,
        cohort=cohort,
        selected_policy_id=selected_id,
        selected_policy_version=selected_version,
        stable_policy_id=deployment.stable_policy_id,
        stable_policy_version=deployment.stable_policy_version,
        reasons=tuple(reasons),
        assigned_at=timestamp,
        policy_snapshot_hash=snapshot_hash,
        runtime_policy_changed=candidate,
    )


@dataclass(frozen=True, slots=True)
class CanaryWindow:
    window_id: str
    deployment_id: str
    stage_pct: int
    candidate_cases: int
    control_cases: int
    candidate_success_rate: float
    control_success_rate: float
    success_delta_ci95_lower_pp: float
    candidate_severe_error_count: int
    control_severe_error_count: int
    hard_budget_violation_count: int
    permission_violation_count: int
    candidate_p95_latency_ms: float
    control_p95_latency_ms: float
    drift_gate_passed: bool
    calibration_gate_passed: bool
    duplicate_external_side_effect_count: int
    high_risk_candidate_cases: int
    explanation_present: bool
    policy_version_present: bool
    rollback_target_present: bool
    observed_at: str
    schema_version: str = CANARY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CANARY_SCHEMA_VERSION:
            raise CanaryControlError("canary observation schema is invalid")
        _anonymous(self.window_id, "canarywin_", "window_id")
        _anonymous(self.deployment_id, "canarydep_", "deployment_id")
        if self.stage_pct not in CANARY_STAGES:
            raise CanaryControlError("canary observation stage is invalid")
        for name in (
            "candidate_cases",
            "control_cases",
            "candidate_severe_error_count",
            "control_severe_error_count",
            "hard_budget_violation_count",
            "permission_violation_count",
            "duplicate_external_side_effect_count",
            "high_risk_candidate_cases",
        ):
            _count(getattr(self, name), name)
        if self.candidate_severe_error_count > self.candidate_cases:
            raise CanaryControlError("candidate severe errors exceed candidate cases")
        if self.control_severe_error_count > self.control_cases:
            raise CanaryControlError("control severe errors exceed control cases")
        for name in ("candidate_success_rate", "control_success_rate"):
            _finite(getattr(self, name), name, minimum=0, maximum=1)
        _finite(self.success_delta_ci95_lower_pp, "success_delta_ci95_lower_pp", minimum=-100, maximum=100)
        _finite(self.candidate_p95_latency_ms, "candidate_p95_latency_ms", minimum=0)
        _finite(self.control_p95_latency_ms, "control_p95_latency_ms", minimum=0)
        for name in (
            "drift_gate_passed",
            "calibration_gate_passed",
            "explanation_present",
            "policy_version_present",
            "rollback_target_present",
        ):
            _strict_bool(getattr(self, name), name)
        _timestamp(self.observed_at, "observed_at")
        expected_id = "canarywin_" + _digest(self.identity())[:32]
        if self.window_id != expected_id:
            raise CanaryControlError("canary observation identity does not match its content")

    def identity(self) -> dict[str, Any]:
        return {
            key: getattr(self, key)
            for key in (
                "deployment_id", "stage_pct", "candidate_cases", "control_cases",
                "candidate_success_rate", "control_success_rate",
                "success_delta_ci95_lower_pp", "candidate_severe_error_count",
                "control_severe_error_count", "hard_budget_violation_count",
                "permission_violation_count", "candidate_p95_latency_ms",
                "control_p95_latency_ms", "drift_gate_passed",
                "calibration_gate_passed", "duplicate_external_side_effect_count",
                "high_risk_candidate_cases", "explanation_present",
                "policy_version_present", "rollback_target_present", "observed_at",
                "schema_version",
            )
        }

    def to_dict(self) -> dict[str, Any]:
        return {"window_id": self.window_id, **self.identity()}

    @classmethod
    def create(cls, **values: Any) -> "CanaryWindow":
        values = dict(values)
        values.setdefault("schema_version", CANARY_SCHEMA_VERSION)
        identity = {key: value for key, value in values.items() if key != "window_id"}
        values["window_id"] = "canarywin_" + _digest(identity)[:32]
        return cls(**values)


@dataclass(frozen=True, slots=True)
class CanaryDecision:
    decision_id: str
    deployment_id: str
    window_id: str
    from_stage_pct: int
    to_stage_pct: int
    action: str
    checks: Mapping[str, bool]
    reasons: tuple[str, ...]
    evaluated_at: str
    runtime_pointer_changed: bool
    automatic_rollback_executed: bool
    in_flight_snapshots_preserved: bool = True
    new_runs_use_decision_immediately: bool = True
    execution_side_effect_count: int = 0
    schema_version: str = CANARY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != CANARY_SCHEMA_VERSION:
            raise CanaryControlError("canary decision schema is invalid")
        _anonymous(self.decision_id, "canarydec_", "decision_id")
        _anonymous(self.deployment_id, "canarydep_", "deployment_id")
        _anonymous(self.window_id, "canarywin_", "window_id")
        if self.from_stage_pct not in CANARY_STAGES or self.to_stage_pct not in CANARY_STAGES:
            raise CanaryControlError("canary decision stage is invalid")
        if self.action not in {"hold", "advance", "rollback", "complete"}:
            raise CanaryControlError("canary decision action is invalid")
        if not isinstance(self.checks, Mapping) or not self.checks:
            raise CanaryControlError("canary decision checks are required")
        if any(not isinstance(value, bool) for value in self.checks.values()):
            raise CanaryControlError("canary decision checks must be boolean")
        _unique(self.reasons, "reasons")
        _timestamp(self.evaluated_at, "evaluated_at")
        for name in (
            "runtime_pointer_changed",
            "automatic_rollback_executed",
            "in_flight_snapshots_preserved",
            "new_runs_use_decision_immediately",
        ):
            _strict_bool(getattr(self, name), name)
        if self.execution_side_effect_count != 0:
            raise CanaryControlError("canary control decisions cannot execute task side effects")
        if self.action == "advance" and self.to_stage_pct != NEXT_STAGE.get(self.from_stage_pct):
            raise CanaryControlError("canary stages cannot be skipped")
        if self.action in {"hold", "rollback", "complete"} and self.to_stage_pct != self.from_stage_pct:
            raise CanaryControlError("non-advance decisions cannot change stage")
        if self.automatic_rollback_executed != (self.action == "rollback"):
            raise CanaryControlError("automatic rollback flag is inconsistent")
        identity = {
            "deployment_id": self.deployment_id,
            "window_id": self.window_id,
            "from_stage_pct": self.from_stage_pct,
            "to_stage_pct": self.to_stage_pct,
            "action": self.action,
            "checks": dict(self.checks),
            "reasons": list(self.reasons),
            "evaluated_at": self.evaluated_at,
        }
        if self.decision_id != "canarydec_" + _digest(identity)[:32]:
            raise CanaryControlError("canary decision identity does not match its content")

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "deployment_id": self.deployment_id,
            "window_id": self.window_id,
            "from_stage_pct": self.from_stage_pct,
            "to_stage_pct": self.to_stage_pct,
            "action": self.action,
            "checks": dict(self.checks),
            "reasons": list(self.reasons),
            "evaluated_at": self.evaluated_at,
            "runtime_pointer_changed": self.runtime_pointer_changed,
            "automatic_rollback_executed": self.automatic_rollback_executed,
            "in_flight_snapshots_preserved": self.in_flight_snapshots_preserved,
            "new_runs_use_decision_immediately": self.new_runs_use_decision_immediately,
            "execution_side_effect_count": self.execution_side_effect_count,
            "schema_version": self.schema_version,
        }


def evaluate_canary_window(
    window: CanaryWindow,
    *,
    evaluated_at: str | None = None,
) -> CanaryDecision:
    """Apply fail-fast guardrails before considering sample sufficiency."""

    candidate_error_rate = (
        window.candidate_severe_error_count / window.candidate_cases
        if window.candidate_cases
        else (1.0 if window.candidate_severe_error_count else 0.0)
    )
    control_error_rate = (
        window.control_severe_error_count / window.control_cases
        if window.control_cases
        else 0.0
    )
    latency_limit = window.control_p95_latency_ms * (1 + MAXIMUM_P95_DEGRADATION_RATIO)
    checks = {
        "success_non_inferiority_ci95": (
            window.success_delta_ci95_lower_pp >= SUCCESS_NON_INFERIORITY_LOWER_PP
        ),
        "severe_error_not_increased": candidate_error_rate <= control_error_rate,
        "hard_budget_violations": window.hard_budget_violation_count == 0,
        "permission_violations": window.permission_violation_count == 0,
        "p95_latency_degradation": window.candidate_p95_latency_ms <= latency_limit,
        "drift_gate": window.drift_gate_passed,
        "calibration_gate": window.calibration_gate_passed,
        "duplicate_external_side_effects": window.duplicate_external_side_effect_count == 0,
        "high_risk_candidate_cases": window.high_risk_candidate_cases == 0,
        "policy_explanation": window.explanation_present,
        "policy_version": window.policy_version_present,
        "rollback_target": window.rollback_target_present,
    }
    failed = tuple(name for name, passed in checks.items() if not passed)
    enough_cases = (
        window.candidate_cases >= MINIMUM_CANDIDATE_CASES[window.stage_pct]
        and window.control_cases > 0
    )
    if failed:
        action = "rollback"
        target = window.stage_pct
        reasons = failed
    elif not enough_cases:
        action = "hold"
        target = window.stage_pct
        reasons = ("minimum_candidate_cases",)
    elif window.stage_pct == 100:
        action = "complete"
        target = window.stage_pct
        reasons = ()
    else:
        action = "advance"
        target = NEXT_STAGE[window.stage_pct]
        reasons = ()
    timestamp = evaluated_at or datetime.now(timezone.utc).isoformat()
    identity = {
        "deployment_id": window.deployment_id,
        "window_id": window.window_id,
        "from_stage_pct": window.stage_pct,
        "to_stage_pct": target,
        "action": action,
        "checks": checks,
        "reasons": list(reasons),
        "evaluated_at": timestamp,
    }
    return CanaryDecision(
        decision_id="canarydec_" + _digest(identity)[:32],
        deployment_id=window.deployment_id,
        window_id=window.window_id,
        from_stage_pct=window.stage_pct,
        to_stage_pct=target,
        action=action,
        checks=MappingProxyType(checks),
        reasons=reasons,
        evaluated_at=timestamp,
        runtime_pointer_changed=action in {"advance", "rollback"},
        automatic_rollback_executed=action == "rollback",
    )


def public_canary_policy() -> dict[str, Any]:
    return {
        "schema_version": CANARY_SCHEMA_VERSION,
        "policy_revision": CANARY_POLICY_REVISION,
        "runtime_enabled": canary_runtime_enabled(),
        "default_enabled": False,
        "stages_pct": list(CANARY_STAGES),
        "minimum_candidate_cases": dict(MINIMUM_CANDIDATE_CASES),
        "guardrails": {
            "success_non_inferiority_ci95_lower_pp": SUCCESS_NON_INFERIORITY_LOWER_PP,
            "severe_error_increase_allowed": False,
            "hard_budget_violations": 0,
            "permission_violations": 0,
            "maximum_p95_degradation_ratio": MAXIMUM_P95_DEGRADATION_RATIO,
            "drift_gate_required": True,
            "calibration_gate_required": True,
            "duplicate_external_side_effects": 0,
            "policy_metadata_required": True,
        },
        "high_risk_exploration_rate": 0.0,
        "elevated_risk_exploration_rate": 0.0,
        "deterministic_assignment": True,
        "in_flight_policy_snapshot_immutable": True,
        "new_runs_observe_rollback_immediately": True,
        "automatic_rollback": True,
        "provider_credentials_granted": False,
        "tool_permissions_expanded": False,
        "external_provider_calls_enabled_by_canary": False,
        "fixture_metrics_are_claimable": False,
        "commercial_claims_allowed": False,
    }


__all__ = [
    "CANARY_ENABLED_ENV",
    "CANARY_POLICY_REVISION",
    "CANARY_SCHEMA_VERSION",
    "CANARY_STAGES",
    "CanaryAssignment",
    "CanaryControlError",
    "CanaryDecision",
    "CanaryDeployment",
    "CanaryWindow",
    "assign_canary_policy",
    "canary_runtime_enabled",
    "evaluate_canary_window",
    "public_canary_policy",
]
