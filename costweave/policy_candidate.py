"""Safe offline policy candidates and counterfactual gates for v0.5.6.5."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .evaluation_protocol import canonical_json
from .predictor_evaluation import (
    PredictorCandidateManifest,
    PredictorEvaluationReport,
)


POLICY_CANDIDATE_SCHEMA_VERSION = "v0.5.6.5"
POLICY_CANDIDATE_POLICY_REVISION = "safe-offline-policy-v1"
OFFLINE_BANDIT_SCHEMA_VERSION = "offline-bandit-evaluation-v1"
POLICY_FREEZE_SCHEMA_VERSION = "policy-freeze-assessment-v1"
POLICY_CARD_SCHEMA_VERSION = "policy-card-v1"

MAXIMUM_ACTIONS = 32
MAXIMUM_OBSERVATIONS = 10_000
DEFAULT_MINIMUM_EFFECTIVE_SAMPLES = 30.0
DEFAULT_MAXIMUM_IMPORTANCE_WEIGHT = 20.0
MAXIMUM_LOW_RISK_EXPLORATION_RATE = 0.05
PSI_FREEZE_THRESHOLD = 0.20
ECE_DEGRADATION_FREEZE_THRESHOLD = 0.03
FAMILY_SUCCESS_DROP_FREEZE_THRESHOLD = -0.05
P95_DEGRADATION_FREEZE_THRESHOLD = 0.20
P95_CONSECUTIVE_WINDOWS = 3

REAL_DATA_ORIGINS = frozenset({"production", "human_benchmark"})
ALL_DATA_ORIGINS = REAL_DATA_ORIGINS | {"test_fixture", "simulated"}
RISK_LEVELS = frozenset({"low", "medium", "high", "critical"})


class PolicyCandidateError(ValueError):
    """Raised when a policy or counterfactual contract is unsafe."""


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
        raise PolicyCandidateError(f"{name} must be numeric") from exc
    if not math.isfinite(number):
        raise PolicyCandidateError(f"{name} must be finite")
    if minimum is not None and number < minimum:
        raise PolicyCandidateError(f"{name} is below its minimum")
    if maximum is not None and number > maximum:
        raise PolicyCandidateError(f"{name} exceeds its maximum")
    return number


def _bounded_identifier(value: str, name: str) -> str:
    text = str(value).strip()
    if not text or len(text) > 160 or "\n" in text or "\r" in text:
        raise PolicyCandidateError(f"{name} must be a bounded identifier")
    lowered = text.lower()
    if any(
        marker in lowered
        for marker in ("bearer ", "api_key=", "password=", "secret=")
    ):
        raise PolicyCandidateError(f"{name} appears to contain a secret")
    return text


def _sha256(value: str, name: str) -> str:
    text = str(value).lower()
    if len(text) != 64 or any(char not in "0123456789abcdef" for char in text):
        raise PolicyCandidateError(f"{name} must be a sha256 digest")
    return text


def _timestamp(value: str, name: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise PolicyCandidateError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise PolicyCandidateError(f"{name} must include a timezone")
    return str(value)


def _evidence_rank(value: str) -> int:
    prefix = str(value).split("-", 1)[0].upper()
    return {"E0": 0, "E1": 1, "E2": 2, "E3": 3, "E4": 4, "E5": 5}.get(
        prefix, -1
    )


def _unique_identifiers(values: Sequence[str], name: str) -> tuple[str, ...]:
    result = tuple(_bounded_identifier(item, name) for item in values)
    if len(result) != len(set(result)):
        raise PolicyCandidateError(f"{name} values must be unique")
    return result


def _anonymous_identifier(value: str, prefix: str, name: str) -> str:
    text = str(value)
    suffix = text.removeprefix(prefix)
    if (
        not text.startswith(prefix)
        or len(suffix) != 32
        or any(char not in "0123456789abcdef" for char in suffix.lower())
    ):
        raise PolicyCandidateError(f"{name} must be an anonymous identifier")
    return text


@dataclass(frozen=True, slots=True)
class PolicyCandidate:
    """An immutable offline policy proposal with no execution authority."""

    candidate_id: str
    predictor_artifact_id: str
    predictor_evaluation_report_id: str
    dataset_content_hash: str
    feature_schema_version: str
    target_schema_version: str
    code_revision: str
    objective_revision: str
    constraint_revision: str
    allowed_task_families: tuple[str, ...]
    prohibited_task_families: tuple[str, ...]
    data_window_start: str
    data_window_end: str
    training_report_id: str
    calibration_report_id: str
    test_report_id: str
    rollback_policy_id: str
    evidence_levels: tuple[str, ...]
    known_strengths: tuple[str, ...]
    failure_modes: tuple[str, ...]
    unsupported_scopes: tuple[str, ...]
    eligible_for_offline_bandit: bool
    status: str = "offline_candidate"
    advisory_only: bool = True
    default_policy: bool = False
    routing_authority: bool = False
    may_change_actual_route: bool = False
    automatic_promotion_allowed: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = POLICY_CANDIDATE_SCHEMA_VERSION
    policy_revision: str = POLICY_CANDIDATE_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_CANDIDATE_SCHEMA_VERSION:
            raise PolicyCandidateError("policy candidate schema is invalid")
        if self.policy_revision != POLICY_CANDIDATE_POLICY_REVISION:
            raise PolicyCandidateError("policy candidate revision is invalid")
        if self.status != "offline_candidate":
            raise PolicyCandidateError("policy candidate must remain offline")
        if (
            not self.advisory_only
            or self.default_policy
            or self.routing_authority
            or self.may_change_actual_route
            or self.automatic_promotion_allowed
            or self.commercial_claims_allowed
        ):
            raise PolicyCandidateError(
                "v0.5.6.5 candidates cannot control or promote routing"
            )
        _sha256(self.dataset_content_hash, "dataset_content_hash")
        for name in (
            "predictor_artifact_id",
            "predictor_evaluation_report_id",
            "feature_schema_version",
            "target_schema_version",
            "code_revision",
            "objective_revision",
            "constraint_revision",
            "training_report_id",
            "calibration_report_id",
            "test_report_id",
            "rollback_policy_id",
        ):
            _bounded_identifier(str(getattr(self, name)), name)
        allowed = _unique_identifiers(
            self.allowed_task_families, "allowed_task_family"
        )
        prohibited = _unique_identifiers(
            self.prohibited_task_families, "prohibited_task_family"
        )
        if not allowed or set(allowed) & set(prohibited):
            raise PolicyCandidateError("policy task family scope is invalid")
        for values, name in (
            (self.evidence_levels, "evidence_level"),
            (self.known_strengths, "known_strength"),
            (self.failure_modes, "failure_mode"),
            (self.unsupported_scopes, "unsupported_scope"),
        ):
            _unique_identifiers(values, name)
        if not self.evidence_levels or min(
            _evidence_rank(item) for item in self.evidence_levels
        ) < 2:
            raise PolicyCandidateError("policy candidate requires E2+ evidence")
        if not self.eligible_for_offline_bandit:
            raise PolicyCandidateError(
                "an offline candidate must be eligible for offline evaluation"
            )
        start = datetime.fromisoformat(
            _timestamp(self.data_window_start, "data_window_start").replace(
                "Z", "+00:00"
            )
        )
        end = datetime.fromisoformat(
            _timestamp(self.data_window_end, "data_window_end").replace(
                "Z", "+00:00"
            )
        )
        if end < start:
            raise PolicyCandidateError("policy data window is reversed")
        expected = "policycand_" + _digest(self.identity())[:32]
        if self.candidate_id != expected:
            raise PolicyCandidateError("policy candidate id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "predictor_artifact_id": self.predictor_artifact_id,
            "predictor_evaluation_report_id": self.predictor_evaluation_report_id,
            "dataset_content_hash": self.dataset_content_hash,
            "feature_schema_version": self.feature_schema_version,
            "target_schema_version": self.target_schema_version,
            "code_revision": self.code_revision,
            "objective_revision": self.objective_revision,
            "constraint_revision": self.constraint_revision,
            "allowed_task_families": list(self.allowed_task_families),
            "prohibited_task_families": list(self.prohibited_task_families),
            "data_window_start": self.data_window_start,
            "data_window_end": self.data_window_end,
            "training_report_id": self.training_report_id,
            "calibration_report_id": self.calibration_report_id,
            "test_report_id": self.test_report_id,
            "rollback_policy_id": self.rollback_policy_id,
            "evidence_levels": list(self.evidence_levels),
            "known_strengths": list(self.known_strengths),
            "failure_modes": list(self.failure_modes),
            "unsupported_scopes": list(self.unsupported_scopes),
            "eligible_for_offline_bandit": self.eligible_for_offline_bandit,
            "status": self.status,
            "advisory_only": self.advisory_only,
            "default_policy": self.default_policy,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"candidate_id": self.candidate_id, **self.identity()}

    @classmethod
    def create(
        cls,
        artifact: PredictorCandidateManifest,
        evaluation: PredictorEvaluationReport,
        *,
        objective_revision: str,
        constraint_revision: str,
        allowed_task_families: Sequence[str],
        prohibited_task_families: Sequence[str],
        data_window_start: str,
        data_window_end: str,
        training_report_id: str,
        calibration_report_id: str,
        test_report_id: str,
        rollback_policy_id: str,
        known_strengths: Sequence[str],
        failure_modes: Sequence[str],
        unsupported_scopes: Sequence[str],
    ) -> "PolicyCandidate":
        if (
            evaluation.predictor_artifact_id != artifact.artifact_id
            or evaluation.dataset_content_hash != artifact.dataset_content_hash
        ):
            raise PolicyCandidateError(
                "policy candidate predictor and evaluation do not match"
            )
        if not evaluation.offline_evaluation_passed:
            raise PolicyCandidateError(
                "failed offline predictor evaluation cannot create a policy"
            )
        if not evaluation.evidence_levels or min(
            _evidence_rank(item) for item in evaluation.evidence_levels
        ) < 2:
            raise PolicyCandidateError("policy candidates require E2+ evidence")
        identity = {
            "predictor_artifact_id": artifact.artifact_id,
            "predictor_evaluation_report_id": evaluation.report_id,
            "dataset_content_hash": artifact.dataset_content_hash,
            "feature_schema_version": artifact.feature_schema_version,
            "target_schema_version": artifact.target_schema_version,
            "code_revision": artifact.code_revision,
            "objective_revision": objective_revision,
            "constraint_revision": constraint_revision,
            "allowed_task_families": list(allowed_task_families),
            "prohibited_task_families": list(prohibited_task_families),
            "data_window_start": data_window_start,
            "data_window_end": data_window_end,
            "training_report_id": training_report_id,
            "calibration_report_id": calibration_report_id,
            "test_report_id": test_report_id,
            "rollback_policy_id": rollback_policy_id,
            "evidence_levels": list(evaluation.evidence_levels),
            "known_strengths": list(known_strengths),
            "failure_modes": list(failure_modes),
            "unsupported_scopes": list(unsupported_scopes),
            "eligible_for_offline_bandit": True,
            "status": "offline_candidate",
            "advisory_only": True,
            "default_policy": False,
            "routing_authority": False,
            "may_change_actual_route": False,
            "automatic_promotion_allowed": False,
            "commercial_claims_allowed": False,
            "schema_version": POLICY_CANDIDATE_SCHEMA_VERSION,
            "policy_revision": POLICY_CANDIDATE_POLICY_REVISION,
        }
        constructor = dict(identity)
        for name in (
            "allowed_task_families",
            "prohibited_task_families",
            "evidence_levels",
            "known_strengths",
            "failure_modes",
            "unsupported_scopes",
        ):
            constructor[name] = tuple(constructor[name])
        return cls(
            candidate_id="policycand_" + _digest(identity)[:32], **constructor
        )


@dataclass(frozen=True, slots=True)
class ExplorationDecision:
    requested_rate: float
    approved_rate: float
    exploration_allowed: bool
    reasons: tuple[str, ...]
    risk_level: str
    reversible: bool
    permission_sensitive: bool
    advisory_only: bool = True
    may_change_actual_route: bool = False

    def __post_init__(self) -> None:
        _finite(self.requested_rate, "requested_rate", minimum=0.0, maximum=1.0)
        _finite(self.approved_rate, "approved_rate", minimum=0.0, maximum=1.0)
        if self.risk_level not in RISK_LEVELS:
            raise PolicyCandidateError("exploration risk level is invalid")
        if not self.advisory_only or self.may_change_actual_route:
            raise PolicyCandidateError("exploration decision cannot execute routes")
        if self.exploration_allowed != (self.approved_rate > 0):
            raise PolicyCandidateError("exploration decision is inconsistent")

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_rate": self.requested_rate,
            "approved_rate": self.approved_rate,
            "exploration_allowed": self.exploration_allowed,
            "reasons": list(self.reasons),
            "risk_level": self.risk_level,
            "reversible": self.reversible,
            "permission_sensitive": self.permission_sensitive,
            "advisory_only": self.advisory_only,
            "may_change_actual_route": self.may_change_actual_route,
        }


def assess_exploration(
    *,
    risk_level: str,
    reversible: bool,
    permission_sensitive: bool,
    requested_rate: float,
) -> ExplorationDecision:
    risk = str(risk_level).lower()
    requested = _finite(
        requested_rate, "requested_rate", minimum=0.0, maximum=1.0
    )
    reasons: list[str] = []
    if risk not in RISK_LEVELS:
        raise PolicyCandidateError("exploration risk level is invalid")
    if risk in {"high", "critical"}:
        reasons.append("high_risk_exploration_forbidden")
    if not reversible:
        reasons.append("irreversible_task_exploration_forbidden")
    if permission_sensitive:
        reasons.append("permission_sensitive_exploration_forbidden")
    if reasons:
        approved = 0.0
    else:
        approved = min(requested, MAXIMUM_LOW_RISK_EXPLORATION_RATE)
        if requested > MAXIMUM_LOW_RISK_EXPLORATION_RATE:
            reasons.append("exploration_rate_capped")
        if approved == 0:
            reasons.append("zero_rate_requested")
    return ExplorationDecision(
        requested_rate=requested,
        approved_rate=approved,
        exploration_allowed=approved > 0,
        reasons=tuple(reasons),
        risk_level=risk,
        reversible=bool(reversible),
        permission_sensitive=bool(permission_sensitive),
    )


def _validate_distribution(
    value: Mapping[str, float], name: str
) -> dict[str, float]:
    if not value or len(value) > MAXIMUM_ACTIONS:
        raise PolicyCandidateError(f"{name} action count is invalid")
    result = {
        _bounded_identifier(action, f"{name}_action"): _finite(
            probability,
            f"{name}_probability",
            minimum=0.0,
            maximum=1.0,
        )
        for action, probability in value.items()
    }
    if len(result) != len(value) or not math.isclose(
        sum(result.values()), 1.0, abs_tol=1e-9
    ):
        raise PolicyCandidateError(f"{name} must sum to one")
    return result


@dataclass(frozen=True, slots=True)
class LoggedBanditObservation:
    observation_id: str
    anonymous_context_id: str
    task_family: str
    risk_level: str
    reversible: bool
    permission_sensitive: bool
    evidence_level: str
    data_origin: str
    logged_action: str
    logging_action_probabilities: Mapping[str, float]
    candidate_action_probabilities: Mapping[str, float]
    predicted_rewards: Mapping[str, float]
    observed_reward: float
    sample_weight: float = 1.0
    schema_version: str = OFFLINE_BANDIT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != OFFLINE_BANDIT_SCHEMA_VERSION:
            raise PolicyCandidateError("bandit observation schema is invalid")
        _anonymous_identifier(
            self.observation_id, "banditobs_", "bandit observation id"
        )
        _anonymous_identifier(
            self.anonymous_context_id, "context_", "anonymous context id"
        )
        _bounded_identifier(self.task_family, "task_family")
        if self.risk_level not in RISK_LEVELS:
            raise PolicyCandidateError("bandit observation risk is invalid")
        if _evidence_rank(self.evidence_level) < 0:
            raise PolicyCandidateError("bandit observation evidence is invalid")
        if self.data_origin not in ALL_DATA_ORIGINS:
            raise PolicyCandidateError("bandit data origin is invalid")
        logging = _validate_distribution(
            self.logging_action_probabilities, "logging_policy"
        )
        candidate = _validate_distribution(
            self.candidate_action_probabilities, "candidate_policy"
        )
        if set(logging) != set(candidate) or set(logging) != set(
            self.predicted_rewards
        ):
            raise PolicyCandidateError(
                "logging, candidate and reward models need identical actions"
            )
        action = _bounded_identifier(self.logged_action, "logged_action")
        if action not in logging or logging[action] <= 0:
            raise PolicyCandidateError(
                "logged action must have positive logging propensity"
            )
        for action_id, reward in self.predicted_rewards.items():
            _bounded_identifier(action_id, "predicted_reward_action")
            _finite(reward, "predicted_reward", minimum=-1e9, maximum=1e9)
        _finite(
            self.observed_reward,
            "observed_reward",
            minimum=-1e9,
            maximum=1e9,
        )
        _finite(self.sample_weight, "sample_weight", minimum=1e-12)
        exploration_forbidden = (
            self.risk_level in {"high", "critical"}
            or not self.reversible
            or self.permission_sensitive
        )
        positive_candidate_actions = sum(
            probability > 1e-12 for probability in candidate.values()
        )
        if exploration_forbidden and positive_candidate_actions != 1:
            raise PolicyCandidateError(
                "high-risk, irreversible or permission-sensitive contexts "
                "must use a deterministic candidate policy"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observation_id": self.observation_id,
            "anonymous_context_id": self.anonymous_context_id,
            "task_family": self.task_family,
            "risk_level": self.risk_level,
            "reversible": self.reversible,
            "permission_sensitive": self.permission_sensitive,
            "evidence_level": self.evidence_level,
            "data_origin": self.data_origin,
            "logged_action": self.logged_action,
            "logging_action_probabilities": dict(
                sorted(self.logging_action_probabilities.items())
            ),
            "candidate_action_probabilities": dict(
                sorted(self.candidate_action_probabilities.items())
            ),
            "predicted_rewards": dict(sorted(self.predicted_rewards.items())),
            "observed_reward": self.observed_reward,
            "sample_weight": self.sample_weight,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True, slots=True)
class OfflineBanditPolicy:
    minimum_effective_samples: float = DEFAULT_MINIMUM_EFFECTIVE_SAMPLES
    maximum_importance_weight: float = DEFAULT_MAXIMUM_IMPORTANCE_WEIGHT
    support_epsilon: float = 1e-9
    revision: str = POLICY_CANDIDATE_POLICY_REVISION

    def __post_init__(self) -> None:
        _finite(
            self.minimum_effective_samples,
            "minimum_effective_samples",
            minimum=1.0,
        )
        _finite(
            self.maximum_importance_weight,
            "maximum_importance_weight",
            minimum=1.0,
        )
        _finite(
            self.support_epsilon,
            "support_epsilon",
            minimum=1e-15,
            maximum=0.1,
        )
        if self.revision != POLICY_CANDIDATE_POLICY_REVISION:
            raise PolicyCandidateError("offline bandit policy revision is invalid")


@dataclass(frozen=True, slots=True)
class OfflineBanditEvaluation:
    evaluation_id: str
    policy_candidate_id: str
    observation_set_hash: str
    observation_count: int
    task_families: tuple[str, ...]
    evidence_levels: tuple[str, ...]
    data_origins: tuple[str, ...]
    estimates: Mapping[str, float | None]
    diagnostics: Mapping[str, float | int | bool]
    checks: Mapping[str, bool]
    common_support: bool
    counterfactual_conclusion_allowed: bool
    eligible_for_v057_shadow_review: bool
    state: str
    freeze_reasons: tuple[str, ...]
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    automatic_promotion_allowed: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = POLICY_CANDIDATE_SCHEMA_VERSION
    estimator_schema_version: str = OFFLINE_BANDIT_SCHEMA_VERSION
    policy_revision: str = POLICY_CANDIDATE_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.schema_version != POLICY_CANDIDATE_SCHEMA_VERSION:
            raise PolicyCandidateError("bandit evaluation schema is invalid")
        if self.estimator_schema_version != OFFLINE_BANDIT_SCHEMA_VERSION:
            raise PolicyCandidateError("bandit estimator schema is invalid")
        _sha256(self.observation_set_hash, "observation_set_hash")
        if self.observation_count <= 0:
            raise PolicyCandidateError("bandit evaluation must contain observations")
        if self.state not in {
            "blocked_scope",
            "blocked_support",
            "blocked_evidence",
            "insufficient_effective_sample",
            "offline_directional",
            "shadow_review_candidate",
        }:
            raise PolicyCandidateError("bandit evaluation state is invalid")
        if (
            not self.advisory_only
            or self.routing_authority
            or self.may_change_actual_route
            or self.automatic_promotion_allowed
            or self.commercial_claims_allowed
        ):
            raise PolicyCandidateError(
                "offline bandit evaluation cannot control production"
            )
        required_checks = {
            "candidate_scope",
            "common_support",
            "importance_weight_bounded",
            "minimum_effective_sample",
            "real_origin_only",
            "e2_or_higher",
            "high_risk_exploration_zero",
        }
        required_estimates = {
            "logging_policy_mean_reward",
            "ips_policy_value",
            "snips_policy_value",
            "doubly_robust_policy_value",
            "doubly_robust_delta",
            "delta_standard_error",
            "delta_lower_95",
            "delta_upper_95",
        }
        required_diagnostics = {
            "total_sample_weight",
            "effective_sample_size",
            "support_coverage_ratio",
            "maximum_importance_weight",
            "unsupported_action_context_pairs",
            "high_risk_exploration_count",
        }
        if set(self.checks) != required_checks:
            raise PolicyCandidateError("bandit evaluation checks are incomplete")
        if set(self.estimates) != required_estimates:
            raise PolicyCandidateError("bandit evaluation estimates are incomplete")
        if set(self.diagnostics) != required_diagnostics:
            raise PolicyCandidateError("bandit evaluation diagnostics are incomplete")
        if not self.task_families or not self.evidence_levels or not self.data_origins:
            raise PolicyCandidateError("bandit evaluation coverage is empty")
        _unique_identifiers(self.task_families, "bandit_task_family")
        _unique_identifiers(self.evidence_levels, "bandit_evidence_level")
        _unique_identifiers(self.data_origins, "bandit_data_origin")
        support_coverage = _finite(
            self.diagnostics["support_coverage_ratio"],
            "support_coverage_ratio",
            minimum=0.0,
            maximum=1.0,
        )
        _finite(
            self.diagnostics["effective_sample_size"],
            "effective_sample_size",
            minimum=0.0,
        )
        _finite(
            self.diagnostics["maximum_importance_weight"],
            "maximum_importance_weight",
            minimum=0.0,
        )
        if self.common_support and not math.isclose(
            support_coverage, 1.0, abs_tol=1e-9
        ):
            raise PolicyCandidateError("common support coverage is inconsistent")
        if self.common_support != bool(self.checks.get("common_support")):
            raise PolicyCandidateError("bandit common support state is inconsistent")
        if self.counterfactual_conclusion_allowed != all(self.checks.values()):
            raise PolicyCandidateError("bandit conclusion state is inconsistent")
        if self.eligible_for_v057_shadow_review and (
            not self.counterfactual_conclusion_allowed
            or min(_evidence_rank(item) for item in self.evidence_levels) < 3
        ):
            raise PolicyCandidateError("bandit shadow eligibility is invalid")
        failed_checks = tuple(
            sorted(name for name, passed in self.checks.items() if not passed)
        )
        if self.freeze_reasons != failed_checks:
            raise PolicyCandidateError("bandit freeze reasons are inconsistent")
        if not self.checks["candidate_scope"] or not self.checks[
            "high_risk_exploration_zero"
        ]:
            expected_state = "blocked_scope"
        elif not self.checks["common_support"] or not self.checks[
            "importance_weight_bounded"
        ]:
            expected_state = "blocked_support"
        elif not self.checks["real_origin_only"] or not self.checks[
            "e2_or_higher"
        ]:
            expected_state = "blocked_evidence"
        elif not self.checks["minimum_effective_sample"]:
            expected_state = "insufficient_effective_sample"
        elif self.eligible_for_v057_shadow_review:
            expected_state = "shadow_review_candidate"
        else:
            expected_state = "offline_directional"
        if self.state != expected_state:
            raise PolicyCandidateError("bandit state does not match its gates")
        expected = "banditeval_" + _digest(self.identity())[:32]
        if self.evaluation_id != expected:
            raise PolicyCandidateError("bandit evaluation id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "policy_candidate_id": self.policy_candidate_id,
            "observation_set_hash": self.observation_set_hash,
            "observation_count": self.observation_count,
            "task_families": list(self.task_families),
            "evidence_levels": list(self.evidence_levels),
            "data_origins": list(self.data_origins),
            "estimates": dict(sorted(self.estimates.items())),
            "diagnostics": dict(sorted(self.diagnostics.items())),
            "checks": dict(sorted(self.checks.items())),
            "common_support": self.common_support,
            "counterfactual_conclusion_allowed": (
                self.counterfactual_conclusion_allowed
            ),
            "eligible_for_v057_shadow_review": (
                self.eligible_for_v057_shadow_review
            ),
            "state": self.state,
            "freeze_reasons": list(self.freeze_reasons),
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "estimator_schema_version": self.estimator_schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"evaluation_id": self.evaluation_id, **self.identity()}


class OfflineBanditEvaluator:
    """Compute IPS/SNIPS/DR only when support and provenance permit it."""

    def __init__(self, policy: OfflineBanditPolicy | None = None) -> None:
        self.policy = policy or OfflineBanditPolicy()

    def evaluate(
        self,
        candidate: PolicyCandidate,
        observations: Sequence[LoggedBanditObservation],
    ) -> OfflineBanditEvaluation:
        items = tuple(sorted(observations, key=lambda row: row.observation_id))
        if not items or len(items) > MAXIMUM_OBSERVATIONS:
            raise PolicyCandidateError("bandit observation count is out of range")
        ids = [item.observation_id for item in items]
        if len(ids) != len(set(ids)):
            raise PolicyCandidateError("bandit observation ids must be unique")

        total_sample_weight = sum(item.sample_weight for item in items)
        weighted_candidate_support = 0.0
        weighted_rho_rewards = 0.0
        weighted_rho = 0.0
        weighted_logging_reward = 0.0
        combined_importance_weights: list[float] = []
        dr_values: list[tuple[float, float]] = []
        maximum_rho = 0.0
        unsupported_pairs = 0

        for item in items:
            supported_mass = 0.0
            for action, candidate_probability in (
                item.candidate_action_probabilities.items()
            ):
                logging_probability = item.logging_action_probabilities[action]
                if candidate_probability <= self.policy.support_epsilon:
                    continue
                if logging_probability > self.policy.support_epsilon:
                    supported_mass += candidate_probability
                else:
                    unsupported_pairs += 1
            weighted_candidate_support += item.sample_weight * supported_mass

            logging_probability = item.logging_action_probabilities[
                item.logged_action
            ]
            candidate_probability = item.candidate_action_probabilities[
                item.logged_action
            ]
            rho = candidate_probability / logging_probability
            maximum_rho = max(maximum_rho, rho)
            weighted_rho = weighted_rho + item.sample_weight * rho
            weighted_rho_rewards += (
                item.sample_weight * rho * item.observed_reward
            )
            weighted_logging_reward += item.sample_weight * item.observed_reward
            combined_importance_weights.append(item.sample_weight * rho)

            direct = sum(
                item.candidate_action_probabilities[action]
                * item.predicted_rewards[action]
                for action in item.candidate_action_probabilities
            )
            corrected = direct + rho * (
                item.observed_reward - item.predicted_rewards[item.logged_action]
            )
            dr_values.append((corrected, item.sample_weight))

        support_coverage = weighted_candidate_support / total_sample_weight
        common_support = unsupported_pairs == 0 and math.isclose(
            support_coverage, 1.0, abs_tol=1e-9
        )
        ips = weighted_rho_rewards / total_sample_weight
        snips = (
            weighted_rho_rewards / weighted_rho if weighted_rho > 1e-12 else None
        )
        dr = sum(value * weight for value, weight in dr_values) / total_sample_weight
        logging_mean = weighted_logging_reward / total_sample_weight
        delta_values = [
            (value - item.observed_reward, weight)
            for (value, weight), item in zip(dr_values, items)
        ]
        delta = sum(value * weight for value, weight in delta_values) / total_sample_weight
        if len(delta_values) > 1:
            variance = sum(
                weight * (value - delta) ** 2 for value, weight in delta_values
            ) / total_sample_weight
            effective_n_for_interval = (
                total_sample_weight**2
                / sum(weight**2 for _, weight in delta_values)
            )
            standard_error = math.sqrt(variance / max(effective_n_for_interval, 1.0))
            lower = delta - 1.96 * standard_error
            upper = delta + 1.96 * standard_error
        else:
            standard_error = None
            lower = None
            upper = None
        weight_sum = sum(combined_importance_weights)
        weight_square_sum = sum(value**2 for value in combined_importance_weights)
        effective_sample_size = (
            weight_sum**2 / weight_square_sum if weight_square_sum > 0 else 0.0
        )

        task_families = tuple(sorted({item.task_family for item in items}))
        evidence_levels = tuple(sorted({item.evidence_level for item in items}))
        data_origins = tuple(sorted({item.data_origin for item in items}))
        allowed_scope = all(
            item.task_family in candidate.allowed_task_families
            and item.task_family not in candidate.prohibited_task_families
            for item in items
        )
        high_risk_zero_exploration = all(
            (
                sum(
                    probability > self.policy.support_epsilon
                    for probability in item.candidate_action_probabilities.values()
                )
                == 1
            )
            for item in items
            if item.risk_level in {"high", "critical"}
            or not item.reversible
            or item.permission_sensitive
        )
        real_origin_only = all(
            item.data_origin in REAL_DATA_ORIGINS for item in items
        )
        e2_or_higher = min(_evidence_rank(item.evidence_level) for item in items) >= 2
        checks = {
            "candidate_scope": allowed_scope,
            "common_support": common_support,
            "importance_weight_bounded": (
                maximum_rho <= self.policy.maximum_importance_weight
            ),
            "minimum_effective_sample": (
                effective_sample_size >= self.policy.minimum_effective_samples
            ),
            "real_origin_only": real_origin_only,
            "e2_or_higher": e2_or_higher,
            "high_risk_exploration_zero": high_risk_zero_exploration,
        }
        conclusion_allowed = all(checks.values())
        shadow_eligible = conclusion_allowed and min(
            _evidence_rank(item.evidence_level) for item in items
        ) >= 3
        if not allowed_scope or not high_risk_zero_exploration:
            state = "blocked_scope"
        elif not common_support or not checks["importance_weight_bounded"]:
            state = "blocked_support"
        elif not real_origin_only or not e2_or_higher:
            state = "blocked_evidence"
        elif not checks["minimum_effective_sample"]:
            state = "insufficient_effective_sample"
        elif shadow_eligible:
            state = "shadow_review_candidate"
        else:
            state = "offline_directional"
        reasons = tuple(sorted(name for name, passed in checks.items() if not passed))
        estimates: dict[str, float | None] = {
            "logging_policy_mean_reward": round(logging_mean, 10),
            "ips_policy_value": round(ips, 10),
            "snips_policy_value": None if snips is None else round(snips, 10),
            "doubly_robust_policy_value": round(dr, 10),
            "doubly_robust_delta": round(delta, 10),
            "delta_standard_error": (
                None if standard_error is None else round(standard_error, 10)
            ),
            "delta_lower_95": None if lower is None else round(lower, 10),
            "delta_upper_95": None if upper is None else round(upper, 10),
        }
        diagnostics: dict[str, float | int | bool] = {
            "total_sample_weight": round(total_sample_weight, 8),
            "effective_sample_size": round(effective_sample_size, 8),
            "support_coverage_ratio": round(support_coverage, 10),
            "maximum_importance_weight": round(maximum_rho, 10),
            "unsupported_action_context_pairs": unsupported_pairs,
            "high_risk_exploration_count": 0,
        }
        observation_set_hash = _digest(
            [item.to_dict() for item in sorted(items, key=lambda row: row.observation_id)]
        )
        identity = {
            "policy_candidate_id": candidate.candidate_id,
            "observation_set_hash": observation_set_hash,
            "observation_count": len(items),
            "task_families": list(task_families),
            "evidence_levels": list(evidence_levels),
            "data_origins": list(data_origins),
            "estimates": dict(sorted(estimates.items())),
            "diagnostics": dict(sorted(diagnostics.items())),
            "checks": dict(sorted(checks.items())),
            "common_support": common_support,
            "counterfactual_conclusion_allowed": conclusion_allowed,
            "eligible_for_v057_shadow_review": shadow_eligible,
            "state": state,
            "freeze_reasons": list(reasons),
            "advisory_only": True,
            "routing_authority": False,
            "may_change_actual_route": False,
            "automatic_promotion_allowed": False,
            "commercial_claims_allowed": False,
            "schema_version": POLICY_CANDIDATE_SCHEMA_VERSION,
            "estimator_schema_version": OFFLINE_BANDIT_SCHEMA_VERSION,
            "policy_revision": self.policy.revision,
        }
        return OfflineBanditEvaluation(
            evaluation_id="banditeval_" + _digest(identity)[:32],
            policy_candidate_id=candidate.candidate_id,
            observation_set_hash=observation_set_hash,
            observation_count=len(items),
            task_families=task_families,
            evidence_levels=evidence_levels,
            data_origins=data_origins,
            estimates=estimates,
            diagnostics=diagnostics,
            checks=checks,
            common_support=common_support,
            counterfactual_conclusion_allowed=conclusion_allowed,
            eligible_for_v057_shadow_review=shadow_eligible,
            state=state,
            freeze_reasons=reasons,
        )


def _three_consecutive_over(values: Sequence[float], threshold: float) -> bool:
    run = 0
    for value in values:
        run = run + 1 if value > threshold else 0
        if run >= P95_CONSECUTIVE_WINDOWS:
            return True
    return False


@dataclass(frozen=True, slots=True)
class PolicySafetySignals:
    dataset_psi: float
    ece_degradation: float
    family_success_rate_deltas: Mapping[str, float]
    cost_p95_degradation_windows: tuple[float, ...]
    latency_p95_degradation_windows: tuple[float, ...]
    data_quality_passed: bool
    privacy_check_passed: bool

    def __post_init__(self) -> None:
        _finite(self.dataset_psi, "dataset_psi", minimum=0.0)
        _finite(self.ece_degradation, "ece_degradation", minimum=-1.0, maximum=1.0)
        for family, delta in self.family_success_rate_deltas.items():
            _bounded_identifier(family, "family_success_delta")
            _finite(delta, "family_success_delta", minimum=-1.0, maximum=1.0)
        for name, values in (
            ("cost_p95_degradation_windows", self.cost_p95_degradation_windows),
            ("latency_p95_degradation_windows", self.latency_p95_degradation_windows),
        ):
            if len(values) > 100:
                raise PolicyCandidateError(f"{name} is too large")
            for value in values:
                _finite(value, name, minimum=-1.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_psi": self.dataset_psi,
            "ece_degradation": self.ece_degradation,
            "family_success_rate_deltas": dict(
                sorted(self.family_success_rate_deltas.items())
            ),
            "cost_p95_degradation_windows": list(
                self.cost_p95_degradation_windows
            ),
            "latency_p95_degradation_windows": list(
                self.latency_p95_degradation_windows
            ),
            "data_quality_passed": self.data_quality_passed,
            "privacy_check_passed": self.privacy_check_passed,
        }


@dataclass(frozen=True, slots=True)
class PolicyFreezeAssessment:
    assessment_id: str
    policy_candidate_id: str
    signals_hash: str
    frozen: bool
    state: str
    triggers: tuple[str, ...]
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    automatic_promotion_allowed: bool = False
    schema_version: str = POLICY_FREEZE_SCHEMA_VERSION
    policy_revision: str = POLICY_CANDIDATE_POLICY_REVISION

    def __post_init__(self) -> None:
        _sha256(self.signals_hash, "signals_hash")
        if self.state not in {"clear", "frozen"} or self.frozen != bool(
            self.triggers
        ):
            raise PolicyCandidateError("policy freeze state is inconsistent")
        if (self.state == "frozen") != self.frozen:
            raise PolicyCandidateError("policy freeze label is inconsistent")
        if (
            not self.advisory_only
            or self.routing_authority
            or self.may_change_actual_route
            or self.automatic_promotion_allowed
        ):
            raise PolicyCandidateError("freeze assessment cannot promote routing")
        expected = "policyfreeze_" + _digest(self.identity())[:32]
        if self.assessment_id != expected:
            raise PolicyCandidateError("policy freeze assessment id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "policy_candidate_id": self.policy_candidate_id,
            "signals_hash": self.signals_hash,
            "frozen": self.frozen,
            "state": self.state,
            "triggers": list(self.triggers),
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"assessment_id": self.assessment_id, **self.identity()}


def assess_policy_freeze(
    candidate: PolicyCandidate, signals: PolicySafetySignals
) -> PolicyFreezeAssessment:
    triggers: list[str] = []
    if signals.dataset_psi > PSI_FREEZE_THRESHOLD:
        triggers.append("dataset_psi_above_0_20")
    if signals.ece_degradation > ECE_DEGRADATION_FREEZE_THRESHOLD:
        triggers.append("ece_degradation_above_0_03")
    for family, delta in sorted(signals.family_success_rate_deltas.items()):
        if delta < FAMILY_SUCCESS_DROP_FREEZE_THRESHOLD:
            triggers.append(f"family_success_drop:{family}")
    if _three_consecutive_over(
        signals.cost_p95_degradation_windows, P95_DEGRADATION_FREEZE_THRESHOLD
    ):
        triggers.append("cost_p95_three_window_degradation")
    if _three_consecutive_over(
        signals.latency_p95_degradation_windows,
        P95_DEGRADATION_FREEZE_THRESHOLD,
    ):
        triggers.append("latency_p95_three_window_degradation")
    if not signals.data_quality_passed:
        triggers.append("data_quality_failed")
    if not signals.privacy_check_passed:
        triggers.append("privacy_check_failed")
    triggers_tuple = tuple(sorted(triggers))
    signals_hash = _digest(signals.to_dict())
    identity = {
        "policy_candidate_id": candidate.candidate_id,
        "signals_hash": signals_hash,
        "frozen": bool(triggers_tuple),
        "state": "frozen" if triggers_tuple else "clear",
        "triggers": list(triggers_tuple),
        "advisory_only": True,
        "routing_authority": False,
        "may_change_actual_route": False,
        "automatic_promotion_allowed": False,
        "schema_version": POLICY_FREEZE_SCHEMA_VERSION,
        "policy_revision": POLICY_CANDIDATE_POLICY_REVISION,
    }
    return PolicyFreezeAssessment(
        assessment_id="policyfreeze_" + _digest(identity)[:32],
        policy_candidate_id=candidate.candidate_id,
        signals_hash=signals_hash,
        frozen=bool(triggers_tuple),
        state="frozen" if triggers_tuple else "clear",
        triggers=triggers_tuple,
    )


@dataclass(frozen=True, slots=True)
class PolicyCard:
    card_id: str
    policy_candidate_id: str
    bandit_evaluation_id: str
    freeze_assessment_id: str
    status: str
    known_strengths: tuple[str, ...]
    failure_modes: tuple[str, ...]
    supported_task_families: tuple[str, ...]
    unsupported_scopes: tuple[str, ...]
    evidence_levels: tuple[str, ...]
    sample_coverage: Mapping[str, float | int]
    expected_reward_delta_interval_95: tuple[float, float] | None
    rollback_policy_id: str
    eligible_for_v057_review: bool
    advisory_only: bool = True
    routing_authority: bool = False
    may_change_actual_route: bool = False
    automatic_promotion_allowed: bool = False
    commercial_claims_allowed: bool = False
    schema_version: str = POLICY_CARD_SCHEMA_VERSION
    policy_revision: str = POLICY_CANDIDATE_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.status not in {"blocked", "frozen", "offline_review"}:
            raise PolicyCandidateError("policy card status is invalid")
        if (
            not self.advisory_only
            or self.routing_authority
            or self.may_change_actual_route
            or self.automatic_promotion_allowed
            or self.commercial_claims_allowed
        ):
            raise PolicyCandidateError("policy card cannot control production")
        if self.eligible_for_v057_review != (self.status == "offline_review"):
            raise PolicyCandidateError("policy card review eligibility is invalid")
        if self.expected_reward_delta_interval_95 is not None:
            lower, upper = self.expected_reward_delta_interval_95
            _finite(lower, "expected_delta_lower")
            _finite(upper, "expected_delta_upper")
            if upper < lower:
                raise PolicyCandidateError("policy benefit interval is reversed")
        expected = "policycard_" + _digest(self.identity())[:32]
        if self.card_id != expected:
            raise PolicyCandidateError("policy card id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "policy_candidate_id": self.policy_candidate_id,
            "bandit_evaluation_id": self.bandit_evaluation_id,
            "freeze_assessment_id": self.freeze_assessment_id,
            "status": self.status,
            "known_strengths": list(self.known_strengths),
            "failure_modes": list(self.failure_modes),
            "supported_task_families": list(self.supported_task_families),
            "unsupported_scopes": list(self.unsupported_scopes),
            "evidence_levels": list(self.evidence_levels),
            "sample_coverage": dict(sorted(self.sample_coverage.items())),
            "expected_reward_delta_interval_95": (
                None
                if self.expected_reward_delta_interval_95 is None
                else list(self.expected_reward_delta_interval_95)
            ),
            "rollback_policy_id": self.rollback_policy_id,
            "eligible_for_v057_review": self.eligible_for_v057_review,
            "advisory_only": self.advisory_only,
            "routing_authority": self.routing_authority,
            "may_change_actual_route": self.may_change_actual_route,
            "automatic_promotion_allowed": self.automatic_promotion_allowed,
            "commercial_claims_allowed": self.commercial_claims_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"card_id": self.card_id, **self.identity()}


def build_policy_card(
    candidate: PolicyCandidate,
    evaluation: OfflineBanditEvaluation,
    freeze: PolicyFreezeAssessment,
) -> PolicyCard:
    if (
        evaluation.policy_candidate_id != candidate.candidate_id
        or freeze.policy_candidate_id != candidate.candidate_id
    ):
        raise PolicyCandidateError("policy card inputs do not share a candidate")
    eligible = evaluation.eligible_for_v057_shadow_review and not freeze.frozen
    if freeze.frozen:
        status = "frozen"
    elif eligible:
        status = "offline_review"
    else:
        status = "blocked"
    lower = evaluation.estimates.get("delta_lower_95")
    upper = evaluation.estimates.get("delta_upper_95")
    interval = (
        (float(lower), float(upper))
        if evaluation.counterfactual_conclusion_allowed
        and lower is not None
        and upper is not None
        else None
    )
    coverage: dict[str, float | int] = {
        "observation_count": evaluation.observation_count,
        "effective_sample_size": float(
            evaluation.diagnostics["effective_sample_size"]
        ),
        "support_coverage_ratio": float(
            evaluation.diagnostics["support_coverage_ratio"]
        ),
        "task_family_count": len(evaluation.task_families),
    }
    identity = {
        "policy_candidate_id": candidate.candidate_id,
        "bandit_evaluation_id": evaluation.evaluation_id,
        "freeze_assessment_id": freeze.assessment_id,
        "status": status,
        "known_strengths": list(candidate.known_strengths),
        "failure_modes": list(candidate.failure_modes),
        "supported_task_families": list(candidate.allowed_task_families),
        "unsupported_scopes": list(candidate.unsupported_scopes),
        "evidence_levels": list(evaluation.evidence_levels),
        "sample_coverage": dict(sorted(coverage.items())),
        "expected_reward_delta_interval_95": (
            None if interval is None else list(interval)
        ),
        "rollback_policy_id": candidate.rollback_policy_id,
        "eligible_for_v057_review": eligible,
        "advisory_only": True,
        "routing_authority": False,
        "may_change_actual_route": False,
        "automatic_promotion_allowed": False,
        "commercial_claims_allowed": False,
        "schema_version": POLICY_CARD_SCHEMA_VERSION,
        "policy_revision": POLICY_CANDIDATE_POLICY_REVISION,
    }
    return PolicyCard(
        card_id="policycard_" + _digest(identity)[:32],
        policy_candidate_id=candidate.candidate_id,
        bandit_evaluation_id=evaluation.evaluation_id,
        freeze_assessment_id=freeze.assessment_id,
        status=status,
        known_strengths=candidate.known_strengths,
        failure_modes=candidate.failure_modes,
        supported_task_families=candidate.allowed_task_families,
        unsupported_scopes=candidate.unsupported_scopes,
        evidence_levels=evaluation.evidence_levels,
        sample_coverage=coverage,
        expected_reward_delta_interval_95=interval,
        rollback_policy_id=candidate.rollback_policy_id,
        eligible_for_v057_review=eligible,
    )


def public_policy_candidate_policy() -> dict[str, Any]:
    return {
        "schema_version": POLICY_CANDIDATE_SCHEMA_VERSION,
        "policy_revision": POLICY_CANDIDATE_POLICY_REVISION,
        "candidate_state": "offline_only",
        "estimators": ["IPS", "SNIPS", "Doubly Robust"],
        "minimum_effective_samples": DEFAULT_MINIMUM_EFFECTIVE_SAMPLES,
        "maximum_importance_weight": DEFAULT_MAXIMUM_IMPORTANCE_WEIGHT,
        "common_support_required": True,
        "real_e2_evidence_required_for_directional_conclusion": True,
        "e3_required_for_v057_shadow_review": True,
        "maximum_low_risk_exploration_rate": (
            MAXIMUM_LOW_RISK_EXPLORATION_RATE
        ),
        "high_risk_exploration_rate": 0.0,
        "irreversible_exploration_rate": 0.0,
        "permission_sensitive_exploration_rate": 0.0,
        "freeze_thresholds": {
            "psi": PSI_FREEZE_THRESHOLD,
            "ece_degradation": ECE_DEGRADATION_FREEZE_THRESHOLD,
            "family_success_drop": abs(FAMILY_SUCCESS_DROP_FREEZE_THRESHOLD),
            "p95_degradation": P95_DEGRADATION_FREEZE_THRESHOLD,
            "p95_consecutive_windows": P95_CONSECUTIVE_WINDOWS,
        },
        "current_evidence": "E0-contract-only",
        "advisory_only": True,
        "routing_authority": False,
        "may_change_actual_route": False,
        "automatic_promotion_enabled": False,
        "external_calls_required": False,
        "fixture_metrics_are_claimable": False,
        "commercial_claims_allowed": False,
    }


__all__ = [
    "DEFAULT_MAXIMUM_IMPORTANCE_WEIGHT",
    "DEFAULT_MINIMUM_EFFECTIVE_SAMPLES",
    "MAXIMUM_LOW_RISK_EXPLORATION_RATE",
    "OFFLINE_BANDIT_SCHEMA_VERSION",
    "POLICY_CANDIDATE_POLICY_REVISION",
    "POLICY_CANDIDATE_SCHEMA_VERSION",
    "ExplorationDecision",
    "LoggedBanditObservation",
    "OfflineBanditEvaluation",
    "OfflineBanditEvaluator",
    "OfflineBanditPolicy",
    "PolicyCandidate",
    "PolicyCandidateError",
    "PolicyCard",
    "PolicyFreezeAssessment",
    "PolicySafetySignals",
    "assess_exploration",
    "assess_policy_freeze",
    "build_policy_card",
    "public_policy_candidate_policy",
]
