"""Offline predictor evaluation and non-promotion gates for v0.5.6.4.2."""

from __future__ import annotations

import hashlib
import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from .evaluation_protocol import canonical_json
from .predictor_admission import (
    PredictorAdmissionError,
    PredictorAdmissionReport,
)


PREDICTOR_EVALUATION_SCHEMA_VERSION = "v0.5.6.4.2"
PREDICTOR_EVALUATION_POLICY_REVISION = "offline-evaluation-gate-v1"
PREDICTOR_ARTIFACT_SCHEMA_VERSION = "predictor-artifact-v1"
PREDICTION_OBSERVATION_SCHEMA_VERSION = "prediction-observation-v1"

OUTCOME_FIRST_PASS = "first_pass_success"
OUTCOME_REPAIRED = "repaired_success"
OUTCOME_UPGRADED = "upgraded_success"
OUTCOME_HUMAN = "human_intervention_success"
OUTCOME_FAILED = "final_failure"
OUTCOME_CATEGORIES = (
    OUTCOME_FIRST_PASS,
    OUTCOME_REPAIRED,
    OUTCOME_UPGRADED,
    OUTCOME_HUMAN,
    OUTCOME_FAILED,
)

ARTIFACT_METHODS = (
    "hierarchical_beta",
    "logistic_regression",
    "isotonic_calibration",
    "quantile_composite",
)


class PredictorEvaluationError(PredictorAdmissionError):
    """Raised when offline evaluation evidence is invalid."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _finite(value: Any, name: str, *, minimum: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise PredictorEvaluationError(f"{name} must be numeric") from exc
    if not math.isfinite(number) or number < minimum:
        raise PredictorEvaluationError(f"{name} is out of range")
    return number


def _evidence_rank(value: str) -> int:
    prefix = str(value).split("-", 1)[0].upper()
    return {"E0": 0, "E1": 1, "E2": 2, "E3": 3, "E4": 4, "E5": 5}.get(
        prefix, -1
    )


def _validate_sha256(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
        raise PredictorEvaluationError(f"{name} must be a sha256 digest")


def _bounded_identifier(value: str, name: str) -> None:
    text = str(value).strip()
    if not text or len(text) > 160 or "\n" in text or "\r" in text:
        raise PredictorEvaluationError(f"{name} must be a bounded identifier")
    lowered = text.lower()
    if any(marker in lowered for marker in ("bearer ", "api_key=", "password=", "secret=")):
        raise PredictorEvaluationError(f"{name} appears to contain a secret")


def _anonymous_identifier(value: str, prefix: str, name: str) -> None:
    suffix = str(value).removeprefix(prefix)
    if (
        not str(value).startswith(prefix)
        or len(suffix) != 32
        or any(char not in "0123456789abcdef" for char in suffix.lower())
    ):
        raise PredictorEvaluationError(f"{name} must be a hashed identifier")


@dataclass(frozen=True, slots=True)
class PredictorCandidateManifest:
    """Immutable metadata for an offline model payload; never grants authority."""

    artifact_id: str
    admission_report_id: str
    dataset_content_hash: str
    method: str
    feature_schema_version: str
    target_schema_version: str
    code_revision: str
    training_config_hash: str
    model_payload_hash: str
    created_at: str
    status: str = "frozen_candidate"
    routing_authority: bool = False
    policy_promotion_allowed: bool = False
    schema_version: str = PREDICTOR_ARTIFACT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.method not in ARTIFACT_METHODS:
            raise PredictorEvaluationError("unsupported predictor method")
        if self.status != "frozen_candidate":
            raise PredictorEvaluationError("predictor artifact must remain frozen")
        if self.routing_authority or self.policy_promotion_allowed:
            raise PredictorEvaluationError(
                "offline predictor artifacts cannot control routing"
            )
        _validate_sha256(self.dataset_content_hash, "dataset_content_hash")
        _validate_sha256(self.training_config_hash, "training_config_hash")
        _validate_sha256(self.model_payload_hash, "model_payload_hash")
        for name in (
            "feature_schema_version",
            "target_schema_version",
            "code_revision",
        ):
            _bounded_identifier(str(getattr(self, name)), name)
        try:
            parsed = datetime.fromisoformat(
                self.created_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            raise PredictorEvaluationError("created_at is invalid") from exc
        if parsed.tzinfo is None:
            raise PredictorEvaluationError("created_at must include a timezone")
        expected = "predictor_" + _digest(self.identity())[:32]
        if self.artifact_id != expected:
            raise PredictorEvaluationError("predictor artifact id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "admission_report_id": self.admission_report_id,
            "dataset_content_hash": self.dataset_content_hash,
            "method": self.method,
            "feature_schema_version": self.feature_schema_version,
            "target_schema_version": self.target_schema_version,
            "code_revision": self.code_revision,
            "training_config_hash": self.training_config_hash,
            "model_payload_hash": self.model_payload_hash,
            "created_at": self.created_at,
            "status": self.status,
            "routing_authority": self.routing_authority,
            "policy_promotion_allowed": self.policy_promotion_allowed,
            "schema_version": self.schema_version,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"artifact_id": self.artifact_id, **self.identity()}

    @classmethod
    def create(
        cls,
        admission: PredictorAdmissionReport,
        *,
        method: str,
        feature_schema_version: str,
        target_schema_version: str,
        code_revision: str,
        training_config_hash: str,
        model_payload_hash: str,
        created_at: str,
    ) -> "PredictorCandidateManifest":
        if not admission.training_allowed:
            raise PredictorEvaluationError(
                "a frozen admission cannot create a predictor artifact"
            )
        identity = {
            "admission_report_id": admission.report_id,
            "dataset_content_hash": admission.dataset_content_hash,
            "method": method,
            "feature_schema_version": feature_schema_version,
            "target_schema_version": target_schema_version,
            "code_revision": code_revision,
            "training_config_hash": training_config_hash,
            "model_payload_hash": model_payload_hash,
            "created_at": created_at,
            "status": "frozen_candidate",
            "routing_authority": False,
            "policy_promotion_allowed": False,
            "schema_version": PREDICTOR_ARTIFACT_SCHEMA_VERSION,
        }
        return cls(
            artifact_id="predictor_" + _digest(identity)[:32],
            admission_report_id=admission.report_id,
            dataset_content_hash=admission.dataset_content_hash,
            method=method,
            feature_schema_version=feature_schema_version,
            target_schema_version=target_schema_version,
            code_revision=code_revision,
            training_config_hash=training_config_hash,
            model_payload_hash=model_payload_hash,
            created_at=created_at,
        )


@dataclass(frozen=True, slots=True)
class PredictionEvaluationObservation:
    observation_id: str
    anonymous_sample_id: str
    anonymous_project_id: str
    partition: str
    evidence_level: str
    risk_level: str
    feature_fingerprint: str
    dataset_content_hash: str
    predicted_outcomes: Mapping[str, float]
    actual_outcome: str
    difficulty_score: float
    predicted_total_cost_p50: float
    predicted_total_cost_p95: float
    actual_total_cost: float
    predicted_latency_p50_ms: float
    predicted_latency_p95_ms: float
    actual_latency_ms: float
    weight: float = 1.0
    schema_version: str = PREDICTION_OBSERVATION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _anonymous_identifier(
            self.observation_id, "predobs_", "prediction observation id"
        )
        _anonymous_identifier(
            self.anonymous_sample_id, "sample_", "anonymous sample id"
        )
        _anonymous_identifier(
            self.anonymous_project_id, "project_", "anonymous project id"
        )
        if self.partition != "test":
            raise PredictorEvaluationError(
                "predictor evaluation observations must use the test partition"
            )
        _anonymous_identifier(
            self.feature_fingerprint, "feature_", "feature fingerprint"
        )
        _validate_sha256(self.dataset_content_hash, "dataset_content_hash")
        if self.actual_outcome not in OUTCOME_CATEGORIES:
            raise PredictorEvaluationError("actual outcome category is invalid")
        if self.risk_level.lower() not in {"low", "medium", "high", "critical"}:
            raise PredictorEvaluationError("risk level is invalid")
        if set(self.predicted_outcomes) != set(OUTCOME_CATEGORIES):
            raise PredictorEvaluationError(
                "prediction must contain exactly five outcome categories"
            )
        probabilities = [
            _finite(self.predicted_outcomes[name], name)
            for name in OUTCOME_CATEGORIES
        ]
        if any(value > 1.0 for value in probabilities) or not math.isclose(
            sum(probabilities), 1.0, rel_tol=0.0, abs_tol=1e-6
        ):
            raise PredictorEvaluationError("outcome probabilities must sum to one")
        difficulty = _finite(self.difficulty_score, "difficulty_score")
        if difficulty > 1.0:
            raise PredictorEvaluationError("difficulty_score must be in [0,1]")
        cost_p50 = _finite(
            self.predicted_total_cost_p50, "predicted_total_cost_p50"
        )
        cost_p95 = _finite(
            self.predicted_total_cost_p95, "predicted_total_cost_p95"
        )
        latency_p50 = _finite(
            self.predicted_latency_p50_ms, "predicted_latency_p50_ms"
        )
        latency_p95 = _finite(
            self.predicted_latency_p95_ms, "predicted_latency_p95_ms"
        )
        _finite(self.actual_total_cost, "actual_total_cost")
        _finite(self.actual_latency_ms, "actual_latency_ms")
        if cost_p95 < cost_p50 or latency_p95 < latency_p50:
            raise PredictorEvaluationError("p95 cannot be lower than p50")
        weight = _finite(self.weight, "weight", minimum=1e-12)
        if weight > 1.0:
            raise PredictorEvaluationError("weight must be in (0,1]")

    @property
    def predicted_success(self) -> float:
        return 1.0 - float(self.predicted_outcomes[OUTCOME_FAILED])

    @property
    def actual_success(self) -> bool:
        return self.actual_outcome != OUTCOME_FAILED

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["predicted_outcomes"] = {
            name: float(self.predicted_outcomes[name])
            for name in OUTCOME_CATEGORIES
        }
        return result


@dataclass(frozen=True, slots=True)
class OfflineEvaluationPolicy:
    minimum_effective_tasks: int = 200
    minimum_high_risk_tasks: int = 20
    ece_maximum: float = 0.08
    brier_maximum: float = 0.18
    high_risk_ece_maximum: float = 0.05
    difficulty_spearman_minimum: float = 0.70
    p95_coverage_minimum: float = 0.90
    p95_coverage_maximum: float = 0.99
    high_risk_false_pass_maximum: float = 0.01
    noninferiority_margin: float = 0.02
    minimum_cost_improvement: float = 0.03
    minimum_latency_improvement: float = 0.10
    bins: int = 10
    revision: str = PREDICTOR_EVALUATION_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.minimum_effective_tasks < 1 or self.minimum_high_risk_tasks < 1:
            raise PredictorEvaluationError("sample thresholds must be positive")
        if self.bins < 2 or self.bins > 100:
            raise PredictorEvaluationError("calibration bins must be in [2,100]")
        bounded = (
            self.ece_maximum,
            self.brier_maximum,
            self.high_risk_ece_maximum,
            self.p95_coverage_minimum,
            self.p95_coverage_maximum,
            self.high_risk_false_pass_maximum,
            self.noninferiority_margin,
            self.minimum_cost_improvement,
            self.minimum_latency_improvement,
        )
        if any(not 0.0 <= float(value) <= 1.0 for value in bounded):
            raise PredictorEvaluationError("evaluation thresholds must be in [0,1]")
        if self.p95_coverage_minimum > self.p95_coverage_maximum:
            raise PredictorEvaluationError("p95 coverage bounds are inverted")
        if not -1.0 <= self.difficulty_spearman_minimum <= 1.0:
            raise PredictorEvaluationError("Spearman threshold is invalid")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _weighted_mean(values: Sequence[tuple[float, float]]) -> float:
    total = sum(weight for _, weight in values)
    if total <= 0:
        return 0.0
    return sum(value * weight for value, weight in values) / total


def _ece(
    observations: Sequence[PredictionEvaluationObservation], bins: int
) -> float:
    groups: list[list[PredictionEvaluationObservation]] = [
        [] for _ in range(bins)
    ]
    for item in observations:
        index = min(bins - 1, int(item.predicted_success * bins))
        groups[index].append(item)
    total = sum(item.weight for item in observations)
    if total <= 0:
        return 0.0
    error = 0.0
    for group in groups:
        weight = sum(item.weight for item in group)
        if weight <= 0:
            continue
        predicted = _weighted_mean(
            [(item.predicted_success, item.weight) for item in group]
        )
        actual = _weighted_mean(
            [(float(item.actual_success), item.weight) for item in group]
        )
        error += weight / total * abs(predicted - actual)
    return error


def _ranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: item[1])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        rank = (index + 1 + end) / 2.0
        for original, _ in ordered[index:end]:
            ranks[original] = rank
        index = end
    return ranks


def _spearman(left: Sequence[float], right: Sequence[float]) -> float:
    if len(left) < 2 or len(left) != len(right):
        return 0.0
    x = _ranks(left)
    y = _ranks(right)
    x_mean = sum(x) / len(x)
    y_mean = sum(y) / len(y)
    numerator = sum((a - x_mean) * (b - y_mean) for a, b in zip(x, y))
    left_scale = math.sqrt(sum((a - x_mean) ** 2 for a in x))
    right_scale = math.sqrt(sum((b - y_mean) ** 2 for b in y))
    if left_scale <= 0 or right_scale <= 0:
        return 0.0
    return numerator / (left_scale * right_scale)


def _wilson(success_rate: float, effective_count: float) -> tuple[float, float]:
    if effective_count <= 0:
        return 0.0, 1.0
    z = 1.96
    denominator = 1.0 + z * z / effective_count
    centre = (success_rate + z * z / (2.0 * effective_count)) / denominator
    radius = z * math.sqrt(
        success_rate * (1.0 - success_rate) / effective_count
        + z * z / (4.0 * effective_count * effective_count)
    ) / denominator
    return max(0.0, centre - radius), min(1.0, centre + radius)


def compute_metrics(
    observations: Sequence[PredictionEvaluationObservation], *, bins: int = 10
) -> dict[str, Any]:
    items = tuple(observations)
    if not items:
        raise PredictorEvaluationError("at least one observation is required")
    total_weight = sum(item.weight for item in items)
    success_rate = _weighted_mean(
        [(float(item.actual_success), item.weight) for item in items]
    )
    success_lower, success_upper = _wilson(success_rate, total_weight)
    high_risk = tuple(
        item for item in items if item.risk_level.lower() in {"high", "critical"}
    )
    high_confidence = tuple(
        item for item in high_risk if item.predicted_success >= 0.90
    )
    high_false_pass = (
        _weighted_mean(
            [(float(not item.actual_success), item.weight) for item in high_confidence]
        )
        if high_confidence
        else 0.0
    )
    success_weight = sum(item.weight for item in items if item.actual_success)
    total_cost = sum(item.actual_total_cost * item.weight for item in items)
    total_latency = sum(item.actual_latency_ms * item.weight for item in items)
    return {
        "effective_task_count": round(total_weight, 6),
        "success_rate": round(success_rate, 8),
        "success_rate_lower_95": round(success_lower, 8),
        "success_rate_upper_95": round(success_upper, 8),
        "brier": round(
            _weighted_mean(
                [
                    (
                        (item.predicted_success - float(item.actual_success)) ** 2,
                        item.weight,
                    )
                    for item in items
                ]
            ),
            8,
        ),
        "ece": round(_ece(items, bins), 8),
        "high_risk_effective_count": round(
            sum(item.weight for item in high_risk), 6
        ),
        "high_risk_ece": (
            round(_ece(high_risk, bins), 8) if high_risk else None
        ),
        "high_risk_false_pass_rate": round(high_false_pass, 8),
        "difficulty_failure_spearman": round(
            _spearman(
                [item.difficulty_score for item in items],
                [float(not item.actual_success) for item in items],
            ),
            8,
        ),
        "cost_p95_coverage": round(
            _weighted_mean(
                [
                    (
                        float(item.actual_total_cost <= item.predicted_total_cost_p95),
                        item.weight,
                    )
                    for item in items
                ]
            ),
            8,
        ),
        "latency_p95_coverage": round(
            _weighted_mean(
                [
                    (
                        float(item.actual_latency_ms <= item.predicted_latency_p95_ms),
                        item.weight,
                    )
                    for item in items
                ]
            ),
            8,
        ),
        "cost_p50_absolute_error": round(
            _weighted_mean(
                [
                    (
                        abs(item.predicted_total_cost_p50 - item.actual_total_cost),
                        item.weight,
                    )
                    for item in items
                ]
            ),
            8,
        ),
        "latency_p50_absolute_error_ms": round(
            _weighted_mean(
                [
                    (
                        abs(item.predicted_latency_p50_ms - item.actual_latency_ms),
                        item.weight,
                    )
                    for item in items
                ]
            ),
            8,
        ),
        "cost_per_accepted_task": (
            round(total_cost / success_weight, 8)
            if success_weight > 0
            else None
        ),
        "time_per_accepted_task_ms": (
            round(total_latency / success_weight, 8)
            if success_weight > 0
            else None
        ),
    }


@dataclass(frozen=True, slots=True)
class PredictorEvaluationReport:
    report_id: str
    predictor_artifact_id: str
    admission_report_id: str
    dataset_content_hash: str
    cohort_hash: str
    state: str
    evidence_levels: tuple[str, ...]
    observation_count: int
    metrics: Mapping[str, Any]
    checks: Mapping[str, bool]
    offline_evaluation_passed: bool
    eligible_for_shadow: bool
    routing_authority: bool
    policy_promotion_allowed: bool
    freeze_reasons: tuple[str, ...]
    schema_version: str = PREDICTOR_EVALUATION_SCHEMA_VERSION
    policy_revision: str = PREDICTOR_EVALUATION_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.routing_authority or self.policy_promotion_allowed:
            raise PredictorEvaluationError(
                "offline evaluation cannot grant production authority"
            )
        if self.state not in {
            "blocked",
            "offline_fail",
            "offline_pass",
            "shadow_candidate",
        }:
            raise PredictorEvaluationError("evaluation state is invalid")
        expected = "predeval_" + _digest(self.identity())[:32]
        if self.report_id != expected:
            raise PredictorEvaluationError("evaluation report id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "predictor_artifact_id": self.predictor_artifact_id,
            "admission_report_id": self.admission_report_id,
            "dataset_content_hash": self.dataset_content_hash,
            "cohort_hash": self.cohort_hash,
            "state": self.state,
            "evidence_levels": list(self.evidence_levels),
            "observation_count": self.observation_count,
            "metrics": dict(self.metrics),
            "checks": dict(sorted(self.checks.items())),
            "offline_evaluation_passed": self.offline_evaluation_passed,
            "eligible_for_shadow": self.eligible_for_shadow,
            "routing_authority": self.routing_authority,
            "policy_promotion_allowed": self.policy_promotion_allowed,
            "freeze_reasons": list(self.freeze_reasons),
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"report_id": self.report_id, **self.identity()}


class PredictorEvaluationGate:
    def __init__(self, policy: OfflineEvaluationPolicy | None = None) -> None:
        self.policy = policy or OfflineEvaluationPolicy()

    def evaluate(
        self,
        artifact: PredictorCandidateManifest,
        admission: PredictorAdmissionReport,
        observations: Sequence[PredictionEvaluationObservation],
        *,
        cohort_hash: str,
    ) -> PredictorEvaluationReport:
        _validate_sha256(cohort_hash, "cohort_hash")
        items = tuple(observations)
        metrics = compute_metrics(items, bins=self.policy.bins)
        evidence_levels = tuple(sorted({item.evidence_level for item in items}))
        sample_ids = [item.anonymous_sample_id for item in items]
        real_only = bool(items) and min(
            _evidence_rank(item.evidence_level) for item in items
        ) >= 2
        min_evidence = min(
            (_evidence_rank(item.evidence_level) for item in items), default=-1
        )
        high_ece = metrics["high_risk_ece"]
        checks = {
            "admission_training_allowed": admission.training_allowed,
            "artifact_matches_admission": (
                artifact.admission_report_id == admission.report_id
                and artifact.dataset_content_hash == admission.dataset_content_hash
            ),
            "dataset_version_matches": all(
                item.dataset_content_hash == admission.dataset_content_hash
                for item in items
            ),
            "test_partition_only": all(item.partition == "test" for item in items),
            "unique_samples": len(sample_ids) == len(set(sample_ids)),
            "real_evidence_only": real_only,
            "minimum_effective_tasks": (
                metrics["effective_task_count"]
                >= self.policy.minimum_effective_tasks
            ),
            "minimum_high_risk_tasks": (
                metrics["high_risk_effective_count"]
                >= self.policy.minimum_high_risk_tasks
            ),
            "brier": metrics["brier"] <= self.policy.brier_maximum,
            "ece": metrics["ece"] <= self.policy.ece_maximum,
            "high_risk_ece": (
                high_ece is not None
                and high_ece <= self.policy.high_risk_ece_maximum
            ),
            "difficulty_ordering": (
                metrics["difficulty_failure_spearman"]
                >= self.policy.difficulty_spearman_minimum
            ),
            "cost_p95_coverage": (
                self.policy.p95_coverage_minimum
                <= metrics["cost_p95_coverage"]
                <= self.policy.p95_coverage_maximum
            ),
            "latency_p95_coverage": (
                self.policy.p95_coverage_minimum
                <= metrics["latency_p95_coverage"]
                <= self.policy.p95_coverage_maximum
            ),
            "high_risk_false_pass": (
                metrics["high_risk_false_pass_rate"]
                <= self.policy.high_risk_false_pass_maximum
            ),
        }
        structural_names = {
            "admission_training_allowed",
            "artifact_matches_admission",
            "dataset_version_matches",
            "test_partition_only",
            "unique_samples",
            "real_evidence_only",
            "minimum_effective_tasks",
            "minimum_high_risk_tasks",
        }
        structural_passed = all(checks[name] for name in structural_names)
        offline_passed = all(checks.values())
        shadow_eligible = offline_passed and min_evidence >= 3
        if not structural_passed:
            state = "blocked"
        elif not offline_passed:
            state = "offline_fail"
        elif shadow_eligible:
            state = "shadow_candidate"
        else:
            state = "offline_pass"
        reasons = [name for name, passed in checks.items() if not passed]
        if offline_passed and not shadow_eligible:
            reasons.append("shadow_requires_e3_or_higher")
        reasons = sorted(set(reasons))
        identity = {
            "predictor_artifact_id": artifact.artifact_id,
            "admission_report_id": admission.report_id,
            "dataset_content_hash": admission.dataset_content_hash,
            "cohort_hash": cohort_hash,
            "state": state,
            "evidence_levels": list(evidence_levels),
            "observation_count": len(items),
            "metrics": metrics,
            "checks": dict(sorted(checks.items())),
            "offline_evaluation_passed": offline_passed,
            "eligible_for_shadow": shadow_eligible,
            "routing_authority": False,
            "policy_promotion_allowed": False,
            "freeze_reasons": reasons,
            "schema_version": PREDICTOR_EVALUATION_SCHEMA_VERSION,
            "policy_revision": self.policy.revision,
        }
        return PredictorEvaluationReport(
            report_id="predeval_" + _digest(identity)[:32],
            predictor_artifact_id=artifact.artifact_id,
            admission_report_id=admission.report_id,
            dataset_content_hash=admission.dataset_content_hash,
            cohort_hash=cohort_hash,
            state=state,
            evidence_levels=evidence_levels,
            observation_count=len(items),
            metrics=metrics,
            checks=checks,
            offline_evaluation_passed=offline_passed,
            eligible_for_shadow=shadow_eligible,
            routing_authority=False,
            policy_promotion_allowed=False,
            freeze_reasons=tuple(reasons),
            policy_revision=self.policy.revision,
        )


@dataclass(frozen=True, slots=True)
class ChallengerComparison:
    comparison_id: str
    champion_report_id: str
    challenger_report_id: str
    checks: Mapping[str, bool]
    deltas: Mapping[str, float]
    eligible_for_shadow_comparison: bool
    routing_authority: bool = False
    policy_promotion_allowed: bool = False
    schema_version: str = PREDICTOR_EVALUATION_SCHEMA_VERSION
    policy_revision: str = PREDICTOR_EVALUATION_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.routing_authority or self.policy_promotion_allowed:
            raise PredictorEvaluationError("comparison cannot promote a policy")
        expected = "predcompare_" + _digest(self.identity())[:32]
        if self.comparison_id != expected:
            raise PredictorEvaluationError("comparison id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "champion_report_id": self.champion_report_id,
            "challenger_report_id": self.challenger_report_id,
            "checks": dict(sorted(self.checks.items())),
            "deltas": dict(sorted(self.deltas.items())),
            "eligible_for_shadow_comparison": self.eligible_for_shadow_comparison,
            "routing_authority": self.routing_authority,
            "policy_promotion_allowed": self.policy_promotion_allowed,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"comparison_id": self.comparison_id, **self.identity()}


def compare_challenger(
    champion: PredictorEvaluationReport,
    challenger: PredictorEvaluationReport,
    *,
    policy: OfflineEvaluationPolicy | None = None,
) -> ChallengerComparison:
    active = policy or OfflineEvaluationPolicy()
    champion_cost = champion.metrics.get("cost_per_accepted_task")
    challenger_cost = challenger.metrics.get("cost_per_accepted_task")
    champion_time = champion.metrics.get("time_per_accepted_task_ms")
    challenger_time = challenger.metrics.get("time_per_accepted_task_ms")
    cost_improvement = (
        (float(champion_cost) - float(challenger_cost)) / max(float(champion_cost), 1e-12)
        if champion_cost is not None and challenger_cost is not None
        else -1.0
    )
    latency_improvement = (
        (float(champion_time) - float(challenger_time)) / max(float(champion_time), 1e-12)
        if champion_time is not None and challenger_time is not None
        else -1.0
    )
    deltas = {
        "success_rate": round(
            float(challenger.metrics["success_rate"])
            - float(champion.metrics["success_rate"]),
            8,
        ),
        "brier": round(
            float(challenger.metrics["brier"])
            - float(champion.metrics["brier"]),
            8,
        ),
        "ece": round(
            float(challenger.metrics["ece"])
            - float(champion.metrics["ece"]),
            8,
        ),
        "cost_improvement_ratio": round(cost_improvement, 8),
        "latency_improvement_ratio": round(latency_improvement, 8),
    }
    checks = {
        "same_cohort": (
            champion.cohort_hash == challenger.cohort_hash
            and champion.observation_count == challenger.observation_count
        ),
        "both_offline_passed": (
            champion.offline_evaluation_passed
            and challenger.offline_evaluation_passed
        ),
        "success_noninferior": (
            float(challenger.metrics["success_rate_lower_95"])
            >= float(champion.metrics["success_rate_lower_95"])
            - active.noninferiority_margin
        ),
        "calibration_not_worse": (
            deltas["brier"] <= 0.01 and deltas["ece"] <= 0.01
        ),
        "high_risk_not_worse": (
            float(challenger.metrics["high_risk_false_pass_rate"])
            <= float(champion.metrics["high_risk_false_pass_rate"])
        ),
        "cost_or_latency_value": (
            cost_improvement >= active.minimum_cost_improvement
            or latency_improvement >= active.minimum_latency_improvement
        ),
        "challenger_shadow_eligible": challenger.eligible_for_shadow,
    }
    eligible = all(checks.values())
    identity = {
        "champion_report_id": champion.report_id,
        "challenger_report_id": challenger.report_id,
        "checks": dict(sorted(checks.items())),
        "deltas": dict(sorted(deltas.items())),
        "eligible_for_shadow_comparison": eligible,
        "routing_authority": False,
        "policy_promotion_allowed": False,
        "schema_version": PREDICTOR_EVALUATION_SCHEMA_VERSION,
        "policy_revision": active.revision,
    }
    return ChallengerComparison(
        comparison_id="predcompare_" + _digest(identity)[:32],
        champion_report_id=champion.report_id,
        challenger_report_id=challenger.report_id,
        checks=checks,
        deltas=deltas,
        eligible_for_shadow_comparison=eligible,
        policy_revision=active.revision,
    )


def public_evaluation_policy() -> dict[str, Any]:
    policy = OfflineEvaluationPolicy()
    return {
        "schema_version": PREDICTOR_EVALUATION_SCHEMA_VERSION,
        "policy_revision": policy.revision,
        "artifact_schema_version": PREDICTOR_ARTIFACT_SCHEMA_VERSION,
        "observation_schema_version": PREDICTION_OBSERVATION_SCHEMA_VERSION,
        "outcome_categories": list(OUTCOME_CATEGORIES),
        "thresholds": policy.to_dict(),
        "current_state": "frozen_no_real_evidence",
        "offline_fixture_metrics_are_claimable": False,
        "shadow_is_execution_authority": False,
        "policy_promotion_enabled": False,
        "external_calls_required": False,
        "commercial_claims_allowed": False,
    }


__all__ = [
    "ARTIFACT_METHODS",
    "ChallengerComparison",
    "OfflineEvaluationPolicy",
    "OUTCOME_CATEGORIES",
    "PREDICTOR_EVALUATION_POLICY_REVISION",
    "PREDICTOR_EVALUATION_SCHEMA_VERSION",
    "PredictionEvaluationObservation",
    "PredictorCandidateManifest",
    "PredictorEvaluationError",
    "PredictorEvaluationGate",
    "PredictorEvaluationReport",
    "compare_challenger",
    "compute_metrics",
    "public_evaluation_policy",
]
