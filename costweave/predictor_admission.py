"""Evidence admission and replay contracts for CostWeave v0.5.6.4.1.

This module intentionally does not fit a model.  It decides whether a governed
outcome dataset is eligible for directional analysis or offline calibration,
and emits an honest frozen receipt when no eligible predictor exists.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from typing import Any, Mapping

from .evaluation_protocol import canonical_json
from .outcome_dataset import OutcomeDataset


PREDICTOR_ADMISSION_SCHEMA_VERSION = "v0.5.6.4.1"
PREDICTOR_ADMISSION_POLICY_REVISION = "predictor-admission-v1"
PREDICTION_CONTRACT_REVISION = "outcome-prediction-contract-v1"

PROFILE_DIRECTIONAL = "directional_evaluation"
PROFILE_INTERNAL = "internal_calibration"
PROFILE_FORMAL = "formal_benchmark"
ADMISSION_PROFILES = (PROFILE_DIRECTIONAL, PROFILE_INTERNAL, PROFILE_FORMAL)

STATE_BLOCKED = "blocked"
STATE_DIRECTIONAL_ONLY = "directional_only"
STATE_CALIBRATION_CANDIDATE = "calibration_candidate"

_EVIDENCE_RANK = {
    "E0": 0,
    "E1": 1,
    "E2": 2,
    "E3": 3,
    "E4": 4,
    "E5": 5,
}


class PredictorAdmissionError(RuntimeError):
    """Raised when a predictor admission or replay contract is invalid."""


class PredictorTrainingFrozenError(PredictorAdmissionError):
    """Raised when training is attempted without an admitted dataset."""


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _rate(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _evidence_prefix(value: str) -> str:
    return str(value).split("-", 1)[0].upper()


def _evidence_rank(value: str) -> int:
    return _EVIDENCE_RANK.get(_evidence_prefix(value), -1)


@dataclass(frozen=True, slots=True)
class AdmissionProfile:
    name: str
    minimum_effective_tasks: int
    minimum_task_families: int
    minimum_per_family: int
    may_fit_candidate: bool
    may_promote_policy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


PROFILES: Mapping[str, AdmissionProfile] = {
    PROFILE_DIRECTIONAL: AdmissionProfile(
        name=PROFILE_DIRECTIONAL,
        minimum_effective_tasks=30,
        minimum_task_families=3,
        minimum_per_family=1,
        may_fit_candidate=False,
    ),
    PROFILE_INTERNAL: AdmissionProfile(
        name=PROFILE_INTERNAL,
        minimum_effective_tasks=200,
        minimum_task_families=4,
        minimum_per_family=10,
        may_fit_candidate=True,
    ),
    PROFILE_FORMAL: AdmissionProfile(
        name=PROFILE_FORMAL,
        minimum_effective_tasks=1000,
        minimum_task_families=8,
        minimum_per_family=100,
        may_fit_candidate=True,
    ),
}


@dataclass(frozen=True, slots=True)
class DatasetEvidenceSummary:
    """The bounded, privacy-safe fields used by the admission decision."""

    dataset_id: str
    dataset_content_hash: str
    sample_count: int
    effective_task_count: float
    evidence_weights: Mapping[str, float]
    partition_counts: Mapping[str, int]
    task_family_counts: Mapping[str, float]
    governance_passed: bool
    source_training_allowed: bool
    label_coverage: float
    label_conflict_rate: float
    project_leakage_count: int
    cost_reconciliation_rate: float
    candidate_route_capture_rate: float
    selection_propensity_rate: float
    version_traceability_rate: float
    source_freeze_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.dataset_id.startswith("dataset_"):
            raise PredictorAdmissionError("dataset id is invalid")
        if len(self.dataset_content_hash) != 64:
            raise PredictorAdmissionError("dataset content hash is invalid")
        if self.sample_count < 0 or self.effective_task_count < 0:
            raise PredictorAdmissionError("dataset counts cannot be negative")

    @classmethod
    def from_dataset(cls, dataset: OutcomeDataset) -> "DatasetEvidenceSummary":
        quality = dataset.quality_report
        evidence_weights: dict[str, float] = {}
        task_family_counts: dict[str, float] = {}
        partition_counts = {name: 0 for name in ("train", "calibration", "test")}
        for sample in dataset.samples:
            level = _evidence_prefix(sample.evidence_level)
            evidence_weights[level] = evidence_weights.get(level, 0.0) + float(
                sample.sample_weight
            )
            family = str(sample.task_features.get("task_type") or "unknown")
            task_family_counts[family] = task_family_counts.get(family, 0.0) + float(
                sample.sample_weight
            )
            partition_counts[sample.partition] = partition_counts.get(
                sample.partition, 0
            ) + 1
        return cls(
            dataset_id=dataset.dataset_id,
            dataset_content_hash=dataset.content_hash,
            sample_count=len(dataset.samples),
            effective_task_count=sum(
                float(item.sample_weight) for item in dataset.samples
            ),
            evidence_weights={
                key: round(value, 6)
                for key, value in sorted(evidence_weights.items())
            },
            partition_counts=dict(sorted(partition_counts.items())),
            task_family_counts={
                key: round(value, 6)
                for key, value in sorted(task_family_counts.items())
            },
            governance_passed=bool(quality.get("governance_passed")),
            source_training_allowed=bool(quality.get("training_allowed")),
            label_coverage=_rate(quality.get("label_coverage")),
            label_conflict_rate=_rate(quality.get("label_conflict_rate")),
            project_leakage_count=int(quality.get("project_leakage_count") or 0),
            cost_reconciliation_rate=_rate(
                quality.get("cost_reconciliation_rate")
            ),
            candidate_route_capture_rate=_rate(
                quality.get("candidate_route_capture_rate")
            ),
            selection_propensity_rate=_rate(
                quality.get("selection_propensity_rate")
            ),
            version_traceability_rate=_rate(
                quality.get("version_traceability_rate")
            ),
            source_freeze_reasons=tuple(
                sorted(str(item) for item in quality.get("freeze_reasons", ()))
            ),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["evidence_weights"] = dict(sorted(self.evidence_weights.items()))
        result["partition_counts"] = dict(sorted(self.partition_counts.items()))
        result["task_family_counts"] = dict(
            sorted(self.task_family_counts.items())
        )
        result["source_freeze_reasons"] = list(self.source_freeze_reasons)
        return result


@dataclass(frozen=True, slots=True)
class PredictorAdmissionReport:
    report_id: str
    dataset_id: str
    dataset_content_hash: str
    profile: str
    state: str
    effective_task_count: float
    eligible_real_task_count: float
    task_family_count: int
    training_allowed: bool
    policy_promotion_allowed: bool
    routing_authority: bool
    freeze_reasons: tuple[str, ...]
    checks: Mapping[str, bool]
    summary_hash: str
    schema_version: str = PREDICTOR_ADMISSION_SCHEMA_VERSION
    policy_revision: str = PREDICTOR_ADMISSION_POLICY_REVISION

    def __post_init__(self) -> None:
        if self.profile not in ADMISSION_PROFILES:
            raise PredictorAdmissionError("unknown admission profile")
        if self.state not in {
            STATE_BLOCKED,
            STATE_DIRECTIONAL_ONLY,
            STATE_CALIBRATION_CANDIDATE,
        }:
            raise PredictorAdmissionError("unknown admission state")
        if self.policy_promotion_allowed or self.routing_authority:
            raise PredictorAdmissionError(
                "v0.5.6.4.1 reports cannot promote or control routing"
            )
        expected = "admission_" + _digest(self.identity())[:32]
        if expected != self.report_id:
            raise PredictorAdmissionError("admission report id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_content_hash": self.dataset_content_hash,
            "profile": self.profile,
            "state": self.state,
            "effective_task_count": round(self.effective_task_count, 6),
            "eligible_real_task_count": round(self.eligible_real_task_count, 6),
            "task_family_count": self.task_family_count,
            "training_allowed": self.training_allowed,
            "policy_promotion_allowed": self.policy_promotion_allowed,
            "routing_authority": self.routing_authority,
            "freeze_reasons": list(self.freeze_reasons),
            "checks": dict(sorted(self.checks.items())),
            "summary_hash": self.summary_hash,
            "schema_version": self.schema_version,
            "policy_revision": self.policy_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"report_id": self.report_id, **self.identity()}


class PredictorAdmissionGate:
    """Apply conservative evidence gates before any predictor can be fitted."""

    def evaluate(
        self,
        summary: DatasetEvidenceSummary,
        *,
        profile: str = PROFILE_INTERNAL,
    ) -> PredictorAdmissionReport:
        if profile not in PROFILES:
            raise PredictorAdmissionError(f"unsupported profile: {profile}")
        policy = PROFILES[profile]
        evidence_weights = {
            _evidence_prefix(key): float(value)
            for key, value in summary.evidence_weights.items()
        }
        simulated_weight = sum(
            value
            for key, value in evidence_weights.items()
            if _evidence_rank(key) < 2
        )
        eligible_real = sum(
            value
            for key, value in evidence_weights.items()
            if _evidence_rank(key) >= 2
        )
        family_values = [
            float(value) for value in summary.task_family_counts.values()
        ]
        checks = {
            "governance_passed": summary.governance_passed,
            "real_evidence_only": simulated_weight <= 1e-9 and eligible_real > 0,
            "minimum_effective_tasks": (
                eligible_real >= policy.minimum_effective_tasks
            ),
            "minimum_task_families": (
                len(family_values) >= policy.minimum_task_families
            ),
            "minimum_per_family": bool(family_values)
            and min(family_values) >= policy.minimum_per_family,
            "all_partitions_present": all(
                int(summary.partition_counts.get(name, 0)) > 0
                for name in ("train", "calibration", "test")
            ),
            "project_partition_isolated": summary.project_leakage_count == 0,
            "label_coverage": summary.label_coverage >= 0.80,
            "label_conflicts_bounded": summary.label_conflict_rate <= 0.05,
            "cost_reconciled": summary.cost_reconciliation_rate >= 1.0,
            "candidate_routes_complete": (
                summary.candidate_route_capture_rate >= 1.0
            ),
            "selection_propensity_complete": (
                summary.selection_propensity_rate >= 1.0
            ),
            "version_traceable": summary.version_traceability_rate >= 1.0,
        }
        reasons = [name for name, passed in checks.items() if not passed]

        if profile == PROFILE_DIRECTIONAL and not reasons:
            state = STATE_DIRECTIONAL_ONLY
            training_allowed = False
            reasons.append("directional_profile_never_trains")
        elif not reasons and policy.may_fit_candidate:
            if summary.source_training_allowed:
                state = STATE_CALIBRATION_CANDIDATE
                training_allowed = True
            else:
                state = STATE_BLOCKED
                training_allowed = False
                reasons.extend(summary.source_freeze_reasons)
                if not summary.source_freeze_reasons:
                    reasons.append("source_dataset_training_frozen")
        else:
            state = STATE_BLOCKED
            training_allowed = False

        reasons = sorted(set(reasons))
        summary_hash = _digest(summary.to_dict())
        identity = {
            "dataset_id": summary.dataset_id,
            "dataset_content_hash": summary.dataset_content_hash,
            "profile": profile,
            "state": state,
            "effective_task_count": round(summary.effective_task_count, 6),
            "eligible_real_task_count": round(eligible_real, 6),
            "task_family_count": len(family_values),
            "training_allowed": training_allowed,
            "policy_promotion_allowed": False,
            "routing_authority": False,
            "freeze_reasons": reasons,
            "checks": dict(sorted(checks.items())),
            "summary_hash": summary_hash,
            "schema_version": PREDICTOR_ADMISSION_SCHEMA_VERSION,
            "policy_revision": PREDICTOR_ADMISSION_POLICY_REVISION,
        }
        return PredictorAdmissionReport(
            report_id="admission_" + _digest(identity)[:32],
            dataset_id=summary.dataset_id,
            dataset_content_hash=summary.dataset_content_hash,
            profile=profile,
            state=state,
            effective_task_count=summary.effective_task_count,
            eligible_real_task_count=eligible_real,
            task_family_count=len(family_values),
            training_allowed=training_allowed,
            policy_promotion_allowed=False,
            routing_authority=False,
            freeze_reasons=tuple(reasons),
            checks=checks,
            summary_hash=summary_hash,
        )

    def evaluate_dataset(
        self,
        dataset: OutcomeDataset,
        *,
        profile: str = PROFILE_INTERNAL,
    ) -> PredictorAdmissionReport:
        return self.evaluate(
            DatasetEvidenceSummary.from_dataset(dataset), profile=profile
        )

    @staticmethod
    def assert_training_allowed(report: PredictorAdmissionReport) -> None:
        if not report.training_allowed:
            reasons = ", ".join(report.freeze_reasons) or "not admitted"
            raise PredictorTrainingFrozenError(
                f"predictor training is frozen: {reasons}"
            )


@dataclass(frozen=True, slots=True)
class PredictorFeatureKey:
    """Replayable routing features; raw prompts and secrets are excluded."""

    task_type: str
    risk_level: str
    difficulty_bucket: str
    deployment_id: str
    model_id: str
    provider: str
    reasoning_effort: str
    prompt_revision: str
    tool_revision: str
    catalog_revision: str
    outcome_schema: str

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            text = str(value).strip()
            if not text:
                raise PredictorAdmissionError(f"{name} is required")
            if len(text) > 160 or "\n" in text or "\r" in text:
                raise PredictorAdmissionError(
                    f"{name} must be a bounded revision or identifier"
                )
            lowered = text.lower()
            if any(
                marker in lowered
                for marker in ("bearer ", "api_key=", "password=", "secret=")
            ):
                raise PredictorAdmissionError(
                    f"{name} appears to contain sensitive material"
                )

    @property
    def fingerprint(self) -> str:
        return "feature_" + _digest(self.to_dict())[:32]

    def to_dict(self) -> dict[str, str]:
        return {key: str(value) for key, value in asdict(self).items()}


@dataclass(frozen=True, slots=True)
class FrozenPredictionReceipt:
    """An explicit non-prediction used while learned evidence is unavailable."""

    receipt_id: str
    feature_fingerprint: str
    admission_report_id: str | None
    status: str
    learned: bool
    routing_authority: bool
    outcome_probabilities: Mapping[str, float] | None
    uncertainty_interval: tuple[float, float]
    fallback_router_revision: str
    freeze_reasons: tuple[str, ...]
    schema_version: str = PREDICTOR_ADMISSION_SCHEMA_VERSION
    contract_revision: str = PREDICTION_CONTRACT_REVISION

    def __post_init__(self) -> None:
        if self.status != "frozen":
            raise PredictorAdmissionError("frozen receipt status is invalid")
        if self.learned or self.routing_authority:
            raise PredictorAdmissionError(
                "a frozen prediction cannot be learned or authoritative"
            )
        if self.outcome_probabilities is not None:
            raise PredictorAdmissionError(
                "frozen predictions cannot invent outcome probabilities"
            )
        if tuple(self.uncertainty_interval) != (0.0, 1.0):
            raise PredictorAdmissionError(
                "frozen predictions must expose maximum uncertainty"
            )
        expected = "prediction_" + _digest(self.identity())[:32]
        if expected != self.receipt_id:
            raise PredictorAdmissionError("prediction receipt id is invalid")

    def identity(self) -> dict[str, Any]:
        return {
            "feature_fingerprint": self.feature_fingerprint,
            "admission_report_id": self.admission_report_id,
            "status": self.status,
            "learned": self.learned,
            "routing_authority": self.routing_authority,
            "outcome_probabilities": self.outcome_probabilities,
            "uncertainty_interval": list(self.uncertainty_interval),
            "fallback_router_revision": self.fallback_router_revision,
            "freeze_reasons": list(self.freeze_reasons),
            "schema_version": self.schema_version,
            "contract_revision": self.contract_revision,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"receipt_id": self.receipt_id, **self.identity()}

    @classmethod
    def create(
        cls,
        feature: PredictorFeatureKey,
        *,
        admission: PredictorAdmissionReport | None = None,
        fallback_router_revision: str = "cross-provider-portfolio-v1",
    ) -> "FrozenPredictionReceipt":
        reasons = tuple(
            admission.freeze_reasons
            if admission is not None
            else ("no_admitted_predictor",)
        )
        identity = {
            "feature_fingerprint": feature.fingerprint,
            "admission_report_id": (
                admission.report_id if admission is not None else None
            ),
            "status": "frozen",
            "learned": False,
            "routing_authority": False,
            "outcome_probabilities": None,
            "uncertainty_interval": [0.0, 1.0],
            "fallback_router_revision": fallback_router_revision,
            "freeze_reasons": list(reasons),
            "schema_version": PREDICTOR_ADMISSION_SCHEMA_VERSION,
            "contract_revision": PREDICTION_CONTRACT_REVISION,
        }
        return cls(
            receipt_id="prediction_" + _digest(identity)[:32],
            feature_fingerprint=feature.fingerprint,
            admission_report_id=(
                admission.report_id if admission is not None else None
            ),
            status="frozen",
            learned=False,
            routing_authority=False,
            outcome_probabilities=None,
            uncertainty_interval=(0.0, 1.0),
            fallback_router_revision=fallback_router_revision,
            freeze_reasons=reasons,
        )


def public_predictor_policy() -> dict[str, Any]:
    return {
        "schema_version": PREDICTOR_ADMISSION_SCHEMA_VERSION,
        "policy_revision": PREDICTOR_ADMISSION_POLICY_REVISION,
        "prediction_contract_revision": PREDICTION_CONTRACT_REVISION,
        "profiles": {
            name: profile.to_dict() for name, profile in PROFILES.items()
        },
        "current_state": "frozen",
        "training_enabled_by_default": False,
        "policy_promotion_enabled": False,
        "routing_authority": "legacy-resource-optimizer",
        "external_calls_required": False,
        "invented_probabilities_allowed": False,
        "commercial_claims_allowed": False,
    }


__all__ = [
    "ADMISSION_PROFILES",
    "DatasetEvidenceSummary",
    "FrozenPredictionReceipt",
    "PREDICTION_CONTRACT_REVISION",
    "PREDICTOR_ADMISSION_POLICY_REVISION",
    "PREDICTOR_ADMISSION_SCHEMA_VERSION",
    "PROFILE_DIRECTIONAL",
    "PROFILE_FORMAL",
    "PROFILE_INTERNAL",
    "PROFILES",
    "PredictorAdmissionError",
    "PredictorAdmissionGate",
    "PredictorAdmissionReport",
    "PredictorFeatureKey",
    "PredictorTrainingFrozenError",
    "public_predictor_policy",
]
