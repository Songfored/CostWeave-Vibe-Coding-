"""Signed policy registry and evidence-gated promotion contracts for v0.5.7.3.

The registry deliberately has no authority over the runtime router.  An
``active`` registry entry is a reviewed asset and rollback target, not a live
production routing change.
"""

from __future__ import annotations

import hashlib
import hmac
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .evaluation_protocol import canonical_json


POLICY_REGISTRY_SCHEMA_VERSION = "v0.5.7.3"
POLICY_REGISTRY_POLICY_REVISION = "policy-registry-promotion-v1"
SIGNATURE_ALGORITHM = "hmac-sha256"

POLICY_STATES = (
    "draft",
    "evaluated",
    "shadow",
    "canary",
    "active",
    "frozen",
    "retired",
)
PROMOTION_STAGES = ("evaluation", "shadow", "canary", "activation")
REAL_DATA_ORIGINS = frozenset({"production", "human_benchmark"})
EVIDENCE_LEVELS = frozenset({"E0", "E1", "E2", "E3", "E4", "E5"})

MINIMUM_CASES = {
    "evaluation": 30,
    "shadow": 30,
    "canary": 200,
    "activation": 1_000,
}
MINIMUM_EVIDENCE_RANK = {
    "evaluation": 2,
    "shadow": 3,
    "canary": 3,
    "activation": 4,
}

FINAL_SUCCESS_NON_INFERIORITY_PP = -1.0
MINIMUM_CPS_IMPROVEMENT_PCT = 15.0
MINIMUM_PARALLEL_P95_IMPROVEMENT_PCT = 15.0
MAXIMUM_HANDOFF_REWORK_RATE = 0.02
MAXIMUM_SHADOW_COST_OVERHEAD_RATIO = 0.10
MINIMUM_MULTIPLANNER_SUCCESS_DELTA_PP = 5.0
MINIMUM_MULTIPLANNER_REGRET_IMPROVEMENT_PCT = 15.0
QUALITY_BASELINE_ID = "strongest-feasible-model"
CPS_BASELINE_ID = "v0.4-fixed-policy"

VALID_TRANSITIONS = {
    "draft": frozenset({"evaluated", "frozen", "retired"}),
    "evaluated": frozenset({"shadow", "frozen", "retired"}),
    "shadow": frozenset({"canary", "frozen", "retired"}),
    "canary": frozenset({"active", "frozen"}),
    "active": frozenset({"frozen", "retired"}),
    "frozen": frozenset({"retired"}),
    "retired": frozenset(),
}
TARGET_GATE_STAGE = {
    "evaluated": "evaluation",
    "shadow": "shadow",
    "canary": "canary",
    "active": "activation",
}


class PolicyRegistryError(ValueError):
    """Raised when a registry contract or transition is unsafe."""


class _FrozenChecks(Mapping[str, bool]):
    """Small immutable mapping used by signed gate reports."""

    __slots__ = ("_items",)

    def __init__(self, values: Mapping[str, bool]) -> None:
        object.__setattr__(
            self,
            "_items",
            tuple((str(key), bool(value)) for key, value in values.items()),
        )

    def __getitem__(self, key: str) -> bool:
        for item_key, value in self._items:
            if item_key == key:
                return value
        raise KeyError(key)

    def __iter__(self):
        return (key for key, _ in self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __setattr__(self, name: str, value: Any) -> None:
        raise TypeError("promotion checks are immutable")


def _strict_booleans(item: Any, *names: str) -> None:
    for name in names:
        if not isinstance(getattr(item, name), bool):
            raise PolicyRegistryError(f"{name} must be boolean")


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _finite(
    value: Any,
    name: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PolicyRegistryError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise PolicyRegistryError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise PolicyRegistryError(f"{name} is below its minimum")
    if maximum is not None and number > maximum:
        raise PolicyRegistryError(f"{name} exceeds its maximum")
    return number


def _bounded(value: str, name: str, *, limit: int = 200) -> str:
    text = str(value).strip()
    if not text or len(text) > limit or "\n" in text or "\r" in text:
        raise PolicyRegistryError(f"{name} must be a bounded identifier")
    lowered = text.lower()
    if any(marker in lowered for marker in ("bearer ", "api_key=", "password=", "secret=")):
        raise PolicyRegistryError(f"{name} appears to contain a secret")
    return text


def _unique(values: Sequence[str], name: str) -> tuple[str, ...]:
    result = tuple(_bounded(item, name) for item in values)
    if len(result) != len(set(result)):
        raise PolicyRegistryError(f"{name} values must be unique")
    return result


def _timestamp(value: str, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyRegistryError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise PolicyRegistryError(f"{name} must include a timezone")
    return str(value)


def _sha256(value: str, name: str) -> str:
    text = str(value).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PolicyRegistryError(f"{name} must be a sha256 digest")
    return text


def _anonymous(value: str, prefix: str, name: str) -> str:
    text = str(value)
    suffix = text.removeprefix(prefix)
    if (
        not text.startswith(prefix)
        or len(suffix) != 32
        or any(char not in "0123456789abcdef" for char in suffix.lower())
    ):
        raise PolicyRegistryError(f"{name} must be an anonymous identifier")
    return text


def _evidence_rank(value: str) -> int:
    return {f"E{number}": number for number in range(6)}.get(str(value).upper(), -1)


def _signing_key(value: bytes) -> bytes:
    if not isinstance(value, bytes) or len(value) < 32:
        raise PolicyRegistryError("signing_key must contain at least 32 bytes")
    return value


@dataclass(frozen=True, slots=True)
class PolicyManifest:
    """Immutable, signed identity and scope for a versioned policy asset."""

    registry_policy_id: str
    policy_name: str
    policy_version: str
    source_candidate_id: str
    source_replay_report_id: str
    source_shadow_report_id: str
    code_revision: str
    objective_revision: str
    constraint_revision: str
    allowed_task_families: tuple[str, ...]
    prohibited_task_families: tuple[str, ...]
    rollback_policy_name: str
    rollback_policy_version: str
    multiplanner_default_requested: bool
    created_at: str
    signer_key_id: str
    content_hash: str
    signature: str
    initial_state: str = "draft"
    signature_algorithm: str = SIGNATURE_ALGORITHM
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    automatic_promotion_allowed: bool = False
    runtime_activation_executed: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = POLICY_REGISTRY_SCHEMA_VERSION
    policy_revision: str = POLICY_REGISTRY_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_REGISTRY_SCHEMA_VERSION:
            raise PolicyRegistryError("policy manifest schema is invalid")
        if self.policy_revision != POLICY_REGISTRY_POLICY_REVISION:
            raise PolicyRegistryError("policy manifest revision is invalid")
        if self.signature_algorithm != SIGNATURE_ALGORITHM:
            raise PolicyRegistryError("unsupported policy signature algorithm")
        if self.initial_state != "draft":
            raise PolicyRegistryError("a registered policy must start as draft")
        _strict_booleans(
            self,
            "multiplanner_default_requested",
            "advisory_only",
            "routing_authority",
            "may_change_actual_route",
            "automatic_promotion_allowed",
            "runtime_activation_executed",
            "commercial_claims_allowed",
        )
        if (
            not self.advisory_only
            or self.routing_authority
            or self.may_change_actual_route
            or self.automatic_promotion_allowed
            or self.runtime_activation_executed
            or self.commercial_claims_allowed
        ):
            raise PolicyRegistryError("the v0.5.7.3 registry cannot control runtime routing")
        for name in (
            "policy_name",
            "policy_version",
            "source_candidate_id",
            "source_replay_report_id",
            "source_shadow_report_id",
            "code_revision",
            "objective_revision",
            "constraint_revision",
            "rollback_policy_name",
            "rollback_policy_version",
            "signer_key_id",
        ):
            _bounded(str(getattr(self, name)), name)
        allowed = _unique(self.allowed_task_families, "allowed_task_family")
        prohibited = _unique(self.prohibited_task_families, "prohibited_task_family")
        if not allowed or set(allowed) & set(prohibited):
            raise PolicyRegistryError("policy task family scope is invalid")
        if (
            self.policy_name == self.rollback_policy_name
            and self.policy_version == self.rollback_policy_version
        ):
            raise PolicyRegistryError("a policy cannot roll back to itself")
        _timestamp(self.created_at, "created_at")
        _sha256(self.content_hash, "content_hash")
        if len(self.signature) != 64 or any(
            char not in "0123456789abcdef" for char in self.signature.lower()
        ):
            raise PolicyRegistryError("signature must be an HMAC-SHA256 digest")
        if self.content_hash != _digest(self.unsigned_payload()):
            raise PolicyRegistryError("policy manifest content hash is invalid")
        expected = "policyreg_" + _digest(
            {"content_hash": self.content_hash, "signature": self.signature}
        )[:32]
        if self.registry_policy_id != expected:
            raise PolicyRegistryError("registry policy id is invalid")

    def unsigned_payload(self) -> dict[str, Any]:
        return {
            "policy_name": self.policy_name,
            "policy_version": self.policy_version,
            "source_candidate_id": self.source_candidate_id,
            "source_replay_report_id": self.source_replay_report_id,
            "source_shadow_report_id": self.source_shadow_report_id,
            "code_revision": self.code_revision,
            "objective_revision": self.objective_revision,
            "constraint_revision": self.constraint_revision,
            "allowed_task_families": list(self.allowed_task_families),
            "prohibited_task_families": list(self.prohibited_task_families),
            "rollback_policy_name": self.rollback_policy_name,
            "rollback_policy_version": self.rollback_policy_version,
            "multiplanner_default_requested": self.multiplanner_default_requested,
            "created_at": self.created_at,
            "signer_key_id": self.signer_key_id,
            "initial_state": self.initial_state,
            "signature_algorithm": self.signature_algorithm,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "runtime_activation_executed": self.runtime_activation_executed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def verify_signature(self, signing_key: bytes) -> bool:
        expected = hmac.new(
            _signing_key(signing_key),
            self.content_hash.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, self.signature)

    def to_dict(self) -> dict[str, Any]:
        return {
            "registry_policy_id": self.registry_policy_id,
            **self.unsigned_payload(),
            "content_hash": self.content_hash,
            "signature": self.signature,
        }

    @classmethod
    def create(
        cls,
        *,
        policy_name: str,
        policy_version: str,
        source_candidate_id: str,
        source_replay_report_id: str,
        source_shadow_report_id: str,
        code_revision: str,
        objective_revision: str,
        constraint_revision: str,
        allowed_task_families: Sequence[str],
        prohibited_task_families: Sequence[str],
        rollback_policy_name: str,
        rollback_policy_version: str,
        multiplanner_default_requested: bool,
        signer_key_id: str,
        signing_key: bytes,
        created_at: str | None = None,
    ) -> "PolicyManifest":
        timestamp = created_at or datetime.now(timezone.utc).isoformat()
        placeholder = cls.__new__(cls)
        values = {
            "registry_policy_id": "policyreg_" + "0" * 32,
            "policy_name": str(policy_name),
            "policy_version": str(policy_version),
            "source_candidate_id": str(source_candidate_id),
            "source_replay_report_id": str(source_replay_report_id),
            "source_shadow_report_id": str(source_shadow_report_id),
            "code_revision": str(code_revision),
            "objective_revision": str(objective_revision),
            "constraint_revision": str(constraint_revision),
            "allowed_task_families": tuple(str(item) for item in allowed_task_families),
            "prohibited_task_families": tuple(str(item) for item in prohibited_task_families),
            "rollback_policy_name": str(rollback_policy_name),
            "rollback_policy_version": str(rollback_policy_version),
            "multiplanner_default_requested": bool(multiplanner_default_requested),
            "created_at": timestamp,
            "signer_key_id": str(signer_key_id),
            "content_hash": "0" * 64,
            "signature": "0" * 64,
        }
        for field_name, value in values.items():
            object.__setattr__(placeholder, field_name, value)
        for field_name, value in (
            ("initial_state", "draft"),
            ("signature_algorithm", SIGNATURE_ALGORITHM),
            ("advisory_only", True),
            ("routing_authority", False),
            ("may_change_actual_route", False),
            ("automatic_promotion_allowed", False),
            ("runtime_activation_executed", False),
            ("commercial_claims_allowed", False),
            ("schema_version", POLICY_REGISTRY_SCHEMA_VERSION),
            ("policy_revision", POLICY_REGISTRY_POLICY_REVISION),
        ):
            object.__setattr__(placeholder, field_name, value)
        content_hash = _digest(placeholder.unsigned_payload())
        signature = hmac.new(
            _signing_key(signing_key), content_hash.encode("ascii"), hashlib.sha256
        ).hexdigest()
        policy_id = "policyreg_" + _digest(
            {"content_hash": content_hash, "signature": signature}
        )[:32]
        return cls(**{**values, "registry_policy_id": policy_id, "content_hash": content_hash, "signature": signature})


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    """A version-bound evidence packet using explicit metric units."""

    evidence_id: str
    registry_policy_id: str
    policy_version: str
    cohort_hash: str
    quality_baseline_id: str
    cps_baseline_id: str
    evidence_level: str
    data_origin: str
    independent_evaluation: bool
    case_count: int
    offline_replay_passed: bool
    production_shadow_passed: bool
    shadow_cost_overhead_p95_ratio: float
    canary_evidence_present: bool
    final_success_delta_pp: float
    final_success_delta_ci95_lower_pp: float
    high_risk_success_delta_pp: float
    cps_improvement_pct: float
    cps_improvement_ci95_lower_pct: float
    parallel_p95_improvement_pct: float
    handoff_rework_rate: float
    hard_budget_violation_count: int
    permission_violation_count: int
    concurrency_violation_count: int
    call_count_violation_count: int
    calibration_passed: bool
    data_quality_passed: bool
    privacy_passed: bool
    drift_passed: bool
    multiplanner_default_requested: bool
    difficult_success_delta_pp: float
    routing_regret_improvement_pct: float
    created_at: str
    advisory_only: bool = True
    may_change_actual_route: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = POLICY_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_REGISTRY_SCHEMA_VERSION:
            raise PolicyRegistryError("promotion evidence schema is invalid")
        _strict_booleans(
            self,
            "independent_evaluation",
            "offline_replay_passed",
            "production_shadow_passed",
            "canary_evidence_present",
            "calibration_passed",
            "data_quality_passed",
            "privacy_passed",
            "drift_passed",
            "multiplanner_default_requested",
            "advisory_only",
            "may_change_actual_route",
            "commercial_claims_allowed",
        )
        if not self.advisory_only or self.may_change_actual_route or self.commercial_claims_allowed:
            raise PolicyRegistryError("promotion evidence is advisory only")
        _anonymous(self.registry_policy_id, "policyreg_", "registry_policy_id")
        _bounded(self.policy_version, "policy_version")
        _sha256(self.cohort_hash, "cohort_hash")
        _bounded(self.quality_baseline_id, "quality_baseline_id")
        _bounded(self.cps_baseline_id, "cps_baseline_id")
        if self.evidence_level not in EVIDENCE_LEVELS:
            raise PolicyRegistryError("evidence_level is invalid")
        if self.data_origin not in REAL_DATA_ORIGINS | {"simulated", "test_fixture"}:
            raise PolicyRegistryError("data_origin is invalid")
        if (
            isinstance(self.case_count, bool)
            or not isinstance(self.case_count, int)
            or self.case_count < 0
        ):
            raise PolicyRegistryError("case_count must be a non-negative integer")
        for name in (
            "hard_budget_violation_count",
            "permission_violation_count",
            "concurrency_violation_count",
            "call_count_violation_count",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise PolicyRegistryError(f"{name} must be a non-negative integer")
        for name in (
            "final_success_delta_pp",
            "final_success_delta_ci95_lower_pp",
            "high_risk_success_delta_pp",
            "cps_improvement_pct",
            "cps_improvement_ci95_lower_pct",
            "parallel_p95_improvement_pct",
            "difficult_success_delta_pp",
            "routing_regret_improvement_pct",
        ):
            _finite(getattr(self, name), name, minimum=-100.0, maximum=100.0)
        _finite(
            self.shadow_cost_overhead_p95_ratio,
            "shadow_cost_overhead_p95_ratio",
            minimum=0.0,
        )
        _finite(self.handoff_rework_rate, "handoff_rework_rate", minimum=0.0, maximum=1.0)
        _timestamp(self.created_at, "created_at")
        expected = "polevd_" + _digest(self.identity())[:32]
        if self.evidence_id != expected:
            raise PolicyRegistryError("promotion evidence id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "registry_policy_id": self.registry_policy_id,
            "policy_version": self.policy_version,
            "cohort_hash": self.cohort_hash,
            "quality_baseline_id": self.quality_baseline_id,
            "cps_baseline_id": self.cps_baseline_id,
            "evidence_level": self.evidence_level,
            "data_origin": self.data_origin,
            "independent_evaluation": self.independent_evaluation,
            "case_count": self.case_count,
            "offline_replay_passed": self.offline_replay_passed,
            "production_shadow_passed": self.production_shadow_passed,
            "shadow_cost_overhead_p95_ratio": self.shadow_cost_overhead_p95_ratio,
            "canary_evidence_present": self.canary_evidence_present,
            "final_success_delta_pp": self.final_success_delta_pp,
            "final_success_delta_ci95_lower_pp": self.final_success_delta_ci95_lower_pp,
            "high_risk_success_delta_pp": self.high_risk_success_delta_pp,
            "cps_improvement_pct": self.cps_improvement_pct,
            "cps_improvement_ci95_lower_pct": self.cps_improvement_ci95_lower_pct,
            "parallel_p95_improvement_pct": self.parallel_p95_improvement_pct,
            "handoff_rework_rate": self.handoff_rework_rate,
            "hard_budget_violation_count": self.hard_budget_violation_count,
            "permission_violation_count": self.permission_violation_count,
            "concurrency_violation_count": self.concurrency_violation_count,
            "call_count_violation_count": self.call_count_violation_count,
            "calibration_passed": self.calibration_passed,
            "data_quality_passed": self.data_quality_passed,
            "privacy_passed": self.privacy_passed,
            "drift_passed": self.drift_passed,
            "multiplanner_default_requested": self.multiplanner_default_requested,
            "difficult_success_delta_pp": self.difficult_success_delta_pp,
            "routing_regret_improvement_pct": self.routing_regret_improvement_pct,
            "created_at": self.created_at,
            "advisory_only": self.advisory_only,
            "may_change_actual_route": self.may_change_actual_route,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"evidence_id": self.evidence_id, **self.identity()}

    @classmethod
    def create(cls, **values: Any) -> "PromotionEvidence":
        payload = {
            **values,
            "registry_policy_id": str(values["registry_policy_id"]),
            "policy_version": str(values["policy_version"]),
            "cohort_hash": str(values["cohort_hash"]),
            "quality_baseline_id": str(values["quality_baseline_id"]),
            "cps_baseline_id": str(values["cps_baseline_id"]),
            "evidence_level": str(values["evidence_level"]),
            "data_origin": str(values["data_origin"]),
            "case_count": int(values["case_count"]),
            "created_at": str(values.get("created_at") or datetime.now(timezone.utc).isoformat()),
        }
        identity = {
            **payload,
            "advisory_only": True,
            "may_change_actual_route": False,
            "commercial_claims_allowed": False,
            "schema_version": POLICY_REGISTRY_SCHEMA_VERSION,
        }
        return cls(
            evidence_id="polevd_" + _digest(identity)[:32],
            **payload,
        )


@dataclass(frozen=True, slots=True)
class PromotionGateReport:
    gate_report_id: str
    registry_policy_id: str
    policy_version: str
    evidence_id: str
    stage: str
    required_checks: tuple[str, ...]
    checks: Mapping[str, bool]
    eligible: bool
    decision_code: str
    reasons: tuple[str, ...]
    evaluated_at: str
    registry_transition_allowed: bool
    routing_authority: bool = False
    may_change_actual_route: bool = False
    runtime_activation_executed: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = POLICY_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_REGISTRY_SCHEMA_VERSION:
            raise PolicyRegistryError("promotion gate schema is invalid")
        _strict_booleans(
            self,
            "eligible",
            "registry_transition_allowed",
            "routing_authority",
            "may_change_actual_route",
            "runtime_activation_executed",
            "commercial_claims_allowed",
        )
        if not isinstance(self.checks, _FrozenChecks):
            if any(not isinstance(value, bool) for value in self.checks.values()):
                raise PolicyRegistryError("promotion check values must be boolean")
            object.__setattr__(self, "checks", _FrozenChecks(self.checks))
        _anonymous(self.registry_policy_id, "policyreg_", "registry_policy_id")
        _anonymous(self.evidence_id, "polevd_", "evidence_id")
        _bounded(self.policy_version, "policy_version")
        if self.stage not in PROMOTION_STAGES:
            raise PolicyRegistryError("promotion stage is invalid")
        required = _unique(self.required_checks, "required_check")
        if not required or set(required) != set(self.checks):
            raise PolicyRegistryError("promotion checks do not match required checks")
        expected_eligible = all(bool(self.checks[item]) for item in required)
        if self.eligible != expected_eligible or self.registry_transition_allowed != expected_eligible:
            raise PolicyRegistryError("promotion eligibility is inconsistent")
        expected_code = f"{self.stage}_{'eligible' if self.eligible else 'blocked'}"
        if self.decision_code != expected_code:
            raise PolicyRegistryError("promotion decision code is invalid")
        failed = tuple(item for item in required if not self.checks[item])
        if self.reasons != failed:
            raise PolicyRegistryError("promotion reasons must identify failed checks")
        if self.routing_authority or self.may_change_actual_route or self.runtime_activation_executed or self.commercial_claims_allowed:
            raise PolicyRegistryError("a promotion gate cannot activate runtime routing")
        _timestamp(self.evaluated_at, "evaluated_at")
        expected = "polgate_" + _digest(self.identity())[:32]
        if self.gate_report_id != expected:
            raise PolicyRegistryError("promotion gate report id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "registry_policy_id": self.registry_policy_id,
            "policy_version": self.policy_version,
            "evidence_id": self.evidence_id,
            "stage": self.stage,
            "required_checks": list(self.required_checks),
            "checks": dict(self.checks),
            "eligible": self.eligible,
            "decision_code": self.decision_code,
            "reasons": list(self.reasons),
            "evaluated_at": self.evaluated_at,
            "registry_transition_allowed": self.registry_transition_allowed,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "runtime_activation_executed": self.runtime_activation_executed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"gate_report_id": self.gate_report_id, **self.identity()}


def evaluate_promotion(
    manifest: PolicyManifest,
    evidence: PromotionEvidence,
    *,
    stage: str,
    evaluated_at: str | None = None,
) -> PromotionGateReport:
    """Evaluate the stage-specific hard gates without changing any policy state."""

    if stage not in PROMOTION_STAGES:
        raise PolicyRegistryError("promotion stage is invalid")
    if evidence.registry_policy_id != manifest.registry_policy_id or evidence.policy_version != manifest.policy_version:
        raise PolicyRegistryError("promotion evidence does not belong to this policy version")
    if evidence.multiplanner_default_requested != manifest.multiplanner_default_requested:
        raise PolicyRegistryError("MultiPlanner intent changed after policy signing")

    checks: dict[str, bool] = {
        "evidence_level": _evidence_rank(evidence.evidence_level) >= MINIMUM_EVIDENCE_RANK[stage],
        "real_data_origin": evidence.data_origin in REAL_DATA_ORIGINS,
        "independent_evaluation": evidence.independent_evaluation,
        "minimum_case_count": evidence.case_count >= MINIMUM_CASES[stage],
        "strongest_feasible_quality_baseline": evidence.quality_baseline_id == QUALITY_BASELINE_ID,
        "v0_4_fixed_cps_baseline": evidence.cps_baseline_id == CPS_BASELINE_ID,
        "data_quality": evidence.data_quality_passed,
        "privacy": evidence.privacy_passed,
        "drift": evidence.drift_passed,
        "calibration": evidence.calibration_passed,
        "offline_replay": evidence.offline_replay_passed,
    }
    if stage in {"shadow", "canary", "activation"}:
        checks.update(
            {
                "production_shadow": evidence.production_shadow_passed,
                "shadow_cost_overhead_p95": evidence.shadow_cost_overhead_p95_ratio <= MAXIMUM_SHADOW_COST_OVERHEAD_RATIO,
            }
        )
    if stage in {"canary", "activation"}:
        checks.update(
            {
                "final_success_non_inferiority_ci95": evidence.final_success_delta_ci95_lower_pp >= FINAL_SUCCESS_NON_INFERIORITY_PP,
                "high_risk_success_non_decline": evidence.high_risk_success_delta_pp >= 0.0,
                "cps_improvement_ci95": evidence.cps_improvement_ci95_lower_pct > MINIMUM_CPS_IMPROVEMENT_PCT,
                "parallel_p95_improvement": evidence.parallel_p95_improvement_pct >= MINIMUM_PARALLEL_P95_IMPROVEMENT_PCT,
                "handoff_rework_rate": evidence.handoff_rework_rate < MAXIMUM_HANDOFF_REWORK_RATE,
                "hard_budget_violations": evidence.hard_budget_violation_count == 0,
                "permission_violations": evidence.permission_violation_count == 0,
                "concurrency_violations": evidence.concurrency_violation_count == 0,
                "call_count_violations": evidence.call_count_violation_count == 0,
                "multiplanner_default_value": (
                    not manifest.multiplanner_default_requested
                    or evidence.difficult_success_delta_pp >= MINIMUM_MULTIPLANNER_SUCCESS_DELTA_PP
                    or evidence.routing_regret_improvement_pct >= MINIMUM_MULTIPLANNER_REGRET_IMPROVEMENT_PCT
                ),
            }
        )
    if stage == "activation":
        checks["canary_evidence"] = evidence.canary_evidence_present

    required = tuple(checks)
    eligible = all(checks.values())
    timestamp = evaluated_at or datetime.now(timezone.utc).isoformat()
    values = {
        "registry_policy_id": manifest.registry_policy_id,
        "policy_version": manifest.policy_version,
        "evidence_id": evidence.evidence_id,
        "stage": stage,
        "required_checks": required,
        "checks": checks,
        "eligible": eligible,
        "decision_code": f"{stage}_{'eligible' if eligible else 'blocked'}",
        "reasons": tuple(item for item in required if not checks[item]),
        "evaluated_at": timestamp,
        "registry_transition_allowed": eligible,
    }
    identity = {
        **values,
        "required_checks": list(required),
        "checks": dict(checks),
        "reasons": list(values["reasons"]),
        "routing_authority": False,
        "may_change_actual_route": False,
        "runtime_activation_executed": False,
        "commercial_claims_allowed": False,
        "schema_version": POLICY_REGISTRY_SCHEMA_VERSION,
    }
    return PromotionGateReport(
        gate_report_id="polgate_" + _digest(identity)[:32],
        **values,
    )


@dataclass(frozen=True, slots=True)
class PolicyTransitionReceipt:
    transition_id: str
    registry_policy_id: str
    policy_version: str
    from_state: str
    to_state: str
    from_revision: int
    to_revision: int
    gate_report_id: str | None
    signature_verified: bool
    approval_id: str | None
    reason_code: str
    transitioned_at: str
    registry_pointer_changed: bool
    runtime_route_changed: bool = False
    execution_side_effect_count: int = 0
    schema_version: str = POLICY_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_REGISTRY_SCHEMA_VERSION:
            raise PolicyRegistryError("policy transition schema is invalid")
        _strict_booleans(
            self,
            "signature_verified",
            "registry_pointer_changed",
            "runtime_route_changed",
        )
        _anonymous(self.registry_policy_id, "policyreg_", "registry_policy_id")
        _bounded(self.policy_version, "policy_version")
        if self.from_state not in POLICY_STATES or self.to_state not in POLICY_STATES:
            raise PolicyRegistryError("policy transition state is invalid")
        if self.to_state not in VALID_TRANSITIONS[self.from_state]:
            raise PolicyRegistryError("policy state transition is not allowed")
        if (
            isinstance(self.from_revision, bool)
            or isinstance(self.to_revision, bool)
            or not isinstance(self.from_revision, int)
            or not isinstance(self.to_revision, int)
            or self.from_revision < 0
            or self.to_revision != self.from_revision + 1
        ):
            raise PolicyRegistryError("policy state revision is invalid")
        if self.to_state in TARGET_GATE_STAGE and not self.gate_report_id:
            raise PolicyRegistryError("promotion transition requires a gate report")
        if self.gate_report_id:
            _anonymous(self.gate_report_id, "polgate_", "gate_report_id")
        if self.to_state in {"canary", "active", "retired"} and not self.approval_id:
            raise PolicyRegistryError("this policy transition requires approval")
        if self.approval_id:
            _bounded(self.approval_id, "approval_id")
        _bounded(self.reason_code, "reason_code")
        _timestamp(self.transitioned_at, "transitioned_at")
        expected_pointer = self.to_state == "active" or (
            self.from_state == "active" and self.to_state in {"frozen", "retired"}
        )
        if self.registry_pointer_changed != expected_pointer:
            raise PolicyRegistryError("registry pointer change is inconsistent")
        if self.runtime_route_changed or self.execution_side_effect_count != 0:
            raise PolicyRegistryError("registry transitions cannot change runtime routing")
        expected = "poltrans_" + _digest(self.identity())[:32]
        if self.transition_id != expected:
            raise PolicyRegistryError("policy transition id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "registry_policy_id": self.registry_policy_id,
            "policy_version": self.policy_version,
            "from_state": self.from_state,
            "to_state": self.to_state,
            "from_revision": self.from_revision,
            "to_revision": self.to_revision,
            "gate_report_id": self.gate_report_id,
            "signature_verified": self.signature_verified,
            "approval_id": self.approval_id,
            "reason_code": self.reason_code,
            "transitioned_at": self.transitioned_at,
            "registry_pointer_changed": self.registry_pointer_changed,
            "runtime_route_changed": self.runtime_route_changed,
            "execution_side_effect_count": self.execution_side_effect_count,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"transition_id": self.transition_id, **self.identity()}


def transition_policy(
    manifest: PolicyManifest,
    *,
    current_state: str,
    current_revision: int,
    target_state: str,
    signature_verified: bool,
    gate_report: PromotionGateReport | None = None,
    approval_id: str | None = None,
    reason_code: str = "promotion",
    transitioned_at: str | None = None,
) -> PolicyTransitionReceipt:
    """Build a concurrency-checkable registry transition receipt."""

    if current_state not in POLICY_STATES or target_state not in POLICY_STATES:
        raise PolicyRegistryError("policy state is invalid")
    if target_state not in VALID_TRANSITIONS[current_state]:
        raise PolicyRegistryError("policy state transition is not allowed")
    if not signature_verified:
        raise PolicyRegistryError("policy signature must be verified before transition")
    expected_stage = TARGET_GATE_STAGE.get(target_state)
    if expected_stage:
        if gate_report is None:
            raise PolicyRegistryError("promotion transition requires a gate report")
        if (
            gate_report.registry_policy_id != manifest.registry_policy_id
            or gate_report.policy_version != manifest.policy_version
            or gate_report.stage != expected_stage
            or not gate_report.eligible
        ):
            raise PolicyRegistryError("promotion gate does not authorize this transition")
    elif gate_report is not None:
        raise PolicyRegistryError("freeze and retirement transitions do not accept a gate report")
    if target_state in {"canary", "active", "retired"} and not approval_id:
        raise PolicyRegistryError("this policy transition requires approval")
    timestamp = transitioned_at or datetime.now(timezone.utc).isoformat()
    values = {
        "registry_policy_id": manifest.registry_policy_id,
        "policy_version": manifest.policy_version,
        "from_state": current_state,
        "to_state": target_state,
        "from_revision": int(current_revision),
        "to_revision": int(current_revision) + 1,
        "gate_report_id": gate_report.gate_report_id if gate_report else None,
        "signature_verified": True,
        "approval_id": approval_id,
        "reason_code": reason_code,
        "transitioned_at": timestamp,
        "registry_pointer_changed": target_state == "active" or (
            current_state == "active" and target_state in {"frozen", "retired"}
        ),
    }
    identity = {
        **values,
        "runtime_route_changed": False,
        "execution_side_effect_count": 0,
        "schema_version": POLICY_REGISTRY_SCHEMA_VERSION,
    }
    return PolicyTransitionReceipt(
        transition_id="poltrans_" + _digest(identity)[:32],
        **values,
    )


def freeze_policy(
    manifest: PolicyManifest,
    *,
    current_state: str,
    current_revision: int,
    signature_verified: bool,
    reason_code: str,
    transitioned_at: str | None = None,
) -> PolicyTransitionReceipt:
    """Build a one-click fail-closed registry freeze receipt."""

    if current_state in {"frozen", "retired"}:
        raise PolicyRegistryError("policy is already frozen or retired")
    return transition_policy(
        manifest,
        current_state=current_state,
        current_revision=current_revision,
        target_state="frozen",
        signature_verified=signature_verified,
        reason_code=reason_code,
        transitioned_at=transitioned_at,
    )


@dataclass(frozen=True, slots=True)
class PolicyRollbackReceipt:
    rollback_id: str
    frozen_policy_id: str
    frozen_policy_version: str
    freeze_transition_id: str
    rollback_policy_id: str
    rollback_policy_version: str
    rollback_policy_state: str
    reason_code: str
    created_at: str
    current_policy_frozen: bool = True
    registry_pointer_changed: bool = True
    runtime_route_changed: bool = False
    runtime_rollback_executed: bool = False
    execution_side_effect_count: int = 0
    schema_version: str = POLICY_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_REGISTRY_SCHEMA_VERSION:
            raise PolicyRegistryError("policy rollback schema is invalid")
        _strict_booleans(
            self,
            "current_policy_frozen",
            "registry_pointer_changed",
            "runtime_route_changed",
            "runtime_rollback_executed",
        )
        _anonymous(self.frozen_policy_id, "policyreg_", "frozen_policy_id")
        _anonymous(self.rollback_policy_id, "policyreg_", "rollback_policy_id")
        _anonymous(self.freeze_transition_id, "poltrans_", "freeze_transition_id")
        _bounded(self.frozen_policy_version, "frozen_policy_version")
        _bounded(self.rollback_policy_version, "rollback_policy_version")
        if self.rollback_policy_state != "active":
            raise PolicyRegistryError("rollback target must be an active reviewed policy")
        _bounded(self.reason_code, "reason_code")
        _timestamp(self.created_at, "created_at")
        if not self.current_policy_frozen or not self.registry_pointer_changed:
            raise PolicyRegistryError("registry rollback must freeze and repoint atomically")
        if self.runtime_route_changed or self.runtime_rollback_executed or self.execution_side_effect_count != 0:
            raise PolicyRegistryError("registry rollback cannot change runtime routing")
        expected = "polrollback_" + _digest(self.identity())[:32]
        if self.rollback_id != expected:
            raise PolicyRegistryError("policy rollback id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "frozen_policy_id": self.frozen_policy_id,
            "frozen_policy_version": self.frozen_policy_version,
            "freeze_transition_id": self.freeze_transition_id,
            "rollback_policy_id": self.rollback_policy_id,
            "rollback_policy_version": self.rollback_policy_version,
            "rollback_policy_state": self.rollback_policy_state,
            "reason_code": self.reason_code,
            "created_at": self.created_at,
            "current_policy_frozen": self.current_policy_frozen,
            "registry_pointer_changed": self.registry_pointer_changed,
            "runtime_route_changed": self.runtime_route_changed,
            "runtime_rollback_executed": self.runtime_rollback_executed,
            "execution_side_effect_count": self.execution_side_effect_count,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"rollback_id": self.rollback_id, **self.identity()}


def build_registry_rollback(
    frozen_policy: PolicyManifest,
    freeze_transition: PolicyTransitionReceipt,
    rollback_policy: PolicyManifest,
    *,
    rollback_policy_state: str,
    reason_code: str,
    created_at: str | None = None,
) -> PolicyRollbackReceipt:
    """Build a registry-only freeze-and-repoint receipt."""

    if (
        freeze_transition.registry_policy_id != frozen_policy.registry_policy_id
        or freeze_transition.to_state != "frozen"
    ):
        raise PolicyRegistryError("rollback requires the current policy freeze receipt")
    if (
        frozen_policy.rollback_policy_name != rollback_policy.policy_name
        or frozen_policy.rollback_policy_version != rollback_policy.policy_version
    ):
        raise PolicyRegistryError("rollback target does not match the signed manifest")
    timestamp = created_at or datetime.now(timezone.utc).isoformat()
    values = {
        "frozen_policy_id": frozen_policy.registry_policy_id,
        "frozen_policy_version": frozen_policy.policy_version,
        "freeze_transition_id": freeze_transition.transition_id,
        "rollback_policy_id": rollback_policy.registry_policy_id,
        "rollback_policy_version": rollback_policy.policy_version,
        "rollback_policy_state": rollback_policy_state,
        "reason_code": reason_code,
        "created_at": timestamp,
    }
    identity = {
        **values,
        "current_policy_frozen": True,
        "registry_pointer_changed": True,
        "runtime_route_changed": False,
        "runtime_rollback_executed": False,
        "execution_side_effect_count": 0,
        "schema_version": POLICY_REGISTRY_SCHEMA_VERSION,
    }
    return PolicyRollbackReceipt(
        rollback_id="polrollback_" + _digest(identity)[:32],
        **values,
    )


@dataclass(frozen=True, slots=True)
class RegistryPolicyCard:
    policy_card_id: str
    registry_policy_id: str
    policy_version: str
    current_state: str
    current_revision: int
    signature_verified: bool
    latest_gate_report_id: str | None
    latest_gate_decision: str
    next_required_stage: str | None
    allowed_task_families: tuple[str, ...]
    prohibited_task_families: tuple[str, ...]
    known_strengths: tuple[str, ...]
    failure_modes: tuple[str, ...]
    unsupported_scopes: tuple[str, ...]
    decision_explanation: tuple[str, ...]
    rollback_policy_name: str
    rollback_policy_version: str
    created_at: str
    advisory_only: bool = True
    routing_authority: bool = False
    runtime_route_changed: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = POLICY_REGISTRY_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_REGISTRY_SCHEMA_VERSION:
            raise PolicyRegistryError("registry policy card schema is invalid")
        _strict_booleans(
            self,
            "signature_verified",
            "advisory_only",
            "routing_authority",
            "runtime_route_changed",
            "commercial_claims_allowed",
        )
        _anonymous(self.registry_policy_id, "policyreg_", "registry_policy_id")
        _bounded(self.policy_version, "policy_version")
        if (
            self.current_state not in POLICY_STATES
            or isinstance(self.current_revision, bool)
            or not isinstance(self.current_revision, int)
            or self.current_revision < 0
        ):
            raise PolicyRegistryError("policy card state is invalid")
        if self.latest_gate_report_id:
            _anonymous(self.latest_gate_report_id, "polgate_", "latest_gate_report_id")
        _bounded(self.latest_gate_decision, "latest_gate_decision")
        if self.next_required_stage is not None and self.next_required_stage not in PROMOTION_STAGES:
            raise PolicyRegistryError("policy card next stage is invalid")
        for values, name in (
            (self.allowed_task_families, "allowed_task_family"),
            (self.prohibited_task_families, "prohibited_task_family"),
            (self.known_strengths, "known_strength"),
            (self.failure_modes, "failure_mode"),
            (self.unsupported_scopes, "unsupported_scope"),
            (self.decision_explanation, "decision_explanation"),
        ):
            _unique(values, name)
        _bounded(self.rollback_policy_name, "rollback_policy_name")
        _bounded(self.rollback_policy_version, "rollback_policy_version")
        _timestamp(self.created_at, "created_at")
        if not self.advisory_only or self.routing_authority or self.runtime_route_changed or self.commercial_claims_allowed:
            raise PolicyRegistryError("policy cards cannot claim runtime authority")
        expected = "polcard_" + _digest(self.identity())[:32]
        if self.policy_card_id != expected:
            raise PolicyRegistryError("registry policy card id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "registry_policy_id": self.registry_policy_id,
            "policy_version": self.policy_version,
            "current_state": self.current_state,
            "current_revision": self.current_revision,
            "signature_verified": self.signature_verified,
            "latest_gate_report_id": self.latest_gate_report_id,
            "latest_gate_decision": self.latest_gate_decision,
            "next_required_stage": self.next_required_stage,
            "allowed_task_families": list(self.allowed_task_families),
            "prohibited_task_families": list(self.prohibited_task_families),
            "known_strengths": list(self.known_strengths),
            "failure_modes": list(self.failure_modes),
            "unsupported_scopes": list(self.unsupported_scopes),
            "decision_explanation": list(self.decision_explanation),
            "rollback_policy_name": self.rollback_policy_name,
            "rollback_policy_version": self.rollback_policy_version,
            "created_at": self.created_at,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "runtime_route_changed": self.runtime_route_changed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"policy_card_id": self.policy_card_id, **self.identity()}


def build_policy_card(
    manifest: PolicyManifest,
    *,
    current_state: str,
    current_revision: int,
    signature_verified: bool,
    latest_gate_report: PromotionGateReport | None,
    known_strengths: Sequence[str],
    failure_modes: Sequence[str],
    unsupported_scopes: Sequence[str],
    created_at: str | None = None,
) -> RegistryPolicyCard:
    """Create an audit-friendly explanation of current policy readiness."""

    if current_state not in POLICY_STATES:
        raise PolicyRegistryError("policy card state is invalid")
    next_stage = {
        "draft": "evaluation",
        "evaluated": "shadow",
        "shadow": "canary",
        "canary": "activation",
    }.get(current_state)
    explanations: list[str] = [
        "signature_verified" if signature_verified else "signature_not_verified",
        f"registry_state_{current_state}",
        "runtime_router_unchanged",
    ]
    if latest_gate_report:
        if latest_gate_report.registry_policy_id != manifest.registry_policy_id:
            raise PolicyRegistryError("policy card gate belongs to another policy")
        explanations.append(latest_gate_report.decision_code)
        explanations.extend(f"blocked_by_{reason}" for reason in latest_gate_report.reasons)
        gate_id = latest_gate_report.gate_report_id
        gate_decision = latest_gate_report.decision_code
    else:
        gate_id = None
        gate_decision = "not_evaluated"
    values = {
        "registry_policy_id": manifest.registry_policy_id,
        "policy_version": manifest.policy_version,
        "current_state": current_state,
        "current_revision": int(current_revision),
        "signature_verified": bool(signature_verified),
        "latest_gate_report_id": gate_id,
        "latest_gate_decision": gate_decision,
        "next_required_stage": next_stage,
        "allowed_task_families": manifest.allowed_task_families,
        "prohibited_task_families": manifest.prohibited_task_families,
        "known_strengths": tuple(str(item) for item in known_strengths),
        "failure_modes": tuple(str(item) for item in failure_modes),
        "unsupported_scopes": tuple(str(item) for item in unsupported_scopes),
        "decision_explanation": tuple(explanations),
        "rollback_policy_name": manifest.rollback_policy_name,
        "rollback_policy_version": manifest.rollback_policy_version,
        "created_at": created_at or datetime.now(timezone.utc).isoformat(),
    }
    identity = {
        **values,
        "allowed_task_families": list(values["allowed_task_families"]),
        "prohibited_task_families": list(values["prohibited_task_families"]),
        "known_strengths": list(values["known_strengths"]),
        "failure_modes": list(values["failure_modes"]),
        "unsupported_scopes": list(values["unsupported_scopes"]),
        "decision_explanation": list(values["decision_explanation"]),
        "advisory_only": True,
        "routing_authority": False,
        "runtime_route_changed": False,
        "commercial_claims_allowed": False,
        "schema_version": POLICY_REGISTRY_SCHEMA_VERSION,
    }
    return RegistryPolicyCard(
        policy_card_id="polcard_" + _digest(identity)[:32],
        **values,
    )


def public_policy_registry_policy() -> dict[str, Any]:
    """Return the public v0.5.7.3 lifecycle and evidence contract."""

    return {
        "schema_version": POLICY_REGISTRY_SCHEMA_VERSION,
        "policy_revision": POLICY_REGISTRY_POLICY_REVISION,
        "states": list(POLICY_STATES),
        "valid_transitions": {
            state: sorted(targets) for state, targets in VALID_TRANSITIONS.items()
        },
        "minimum_cases": dict(MINIMUM_CASES),
        "minimum_evidence_rank": dict(MINIMUM_EVIDENCE_RANK),
        "promotion_gates": {
            "quality_baseline_id": QUALITY_BASELINE_ID,
            "cps_baseline_id": CPS_BASELINE_ID,
            "final_success_non_inferiority_ci95_lower_pp": FINAL_SUCCESS_NON_INFERIORITY_PP,
            "high_risk_success_non_decline": True,
            "cps_improvement_ci95_lower_pct_strictly_greater_than": MINIMUM_CPS_IMPROVEMENT_PCT,
            "parallel_p95_improvement_pct": MINIMUM_PARALLEL_P95_IMPROVEMENT_PCT,
            "handoff_rework_rate_strictly_less_than": MAXIMUM_HANDOFF_REWORK_RATE,
            "hard_violation_counts": 0,
            "shadow_cost_overhead_p95_ratio": MAXIMUM_SHADOW_COST_OVERHEAD_RATIO,
        },
        "multiplanner_default_gate": {
            "difficult_success_delta_pp": MINIMUM_MULTIPLANNER_SUCCESS_DELTA_PP,
            "or_routing_regret_improvement_pct": MINIMUM_MULTIPLANNER_REGRET_IMPROVEMENT_PCT,
        },
        "signature_algorithm": SIGNATURE_ALGORITHM,
        "minimum_signing_key_bytes": 32,
        "one_click_freeze": True,
        "registry_rollback": True,
        "immutable_event_log": True,
        "registry_active_is_runtime_active": False,
        "actual_route_changed": False,
        "routing_authority": False,
        "runtime_activation_executed": False,
        "automatic_promotion_allowed": False,
        "commercial_claims_allowed": False,
    }


__all__ = [
    "POLICY_REGISTRY_POLICY_REVISION",
    "POLICY_REGISTRY_SCHEMA_VERSION",
    "POLICY_STATES",
    "PolicyManifest",
    "PolicyRegistryError",
    "PolicyRollbackReceipt",
    "PolicyTransitionReceipt",
    "PromotionEvidence",
    "PromotionGateReport",
    "RegistryPolicyCard",
    "build_policy_card",
    "build_registry_rollback",
    "evaluate_promotion",
    "freeze_policy",
    "public_policy_registry_policy",
    "transition_policy",
]
