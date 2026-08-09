"""Evidence and statistical claim gates for v0.6.0 benchmark reports."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..intelligence.common import EVIDENCE_LEVELS, bounded_text, unique_texts
from .metrics import PolicyMetricsV1
from .statistics import BootstrapInterval


QUALITY_NON_INFERIORITY_PP = -2.0
MINIMUM_COST_REDUCTION_PCT = 10.0
MINIMUM_QUALITY_IMPROVEMENT_PP = 3.0
MAXIMUM_P95_LATENCY_REGRESSION_PCT = 10.0


@dataclass(frozen=True, slots=True)
class ClaimGateResultV1:
    status: str
    passed: bool
    path: str | None
    reason_codes: tuple[str, ...]
    case_count: int
    evidence_level: str
    partition: str
    candidate_policy_id: str
    baseline_policy_id: str

    def __post_init__(self) -> None:
        if self.status not in {"claimable", "insufficient_sample", "not_supported", "invalid"}:
            raise ValueError("unsupported claim status")
        if not isinstance(self.passed, bool):
            raise TypeError("passed must be bool")
        if self.passed != (self.status == "claimable"):
            raise ValueError("passed and status are inconsistent")
        if self.path is not None:
            object.__setattr__(self, "path", bounded_text(self.path, "path"))
        reasons = unique_texts(self.reason_codes, "reason_codes", maximum_items=64)
        if not reasons:
            raise ValueError("reason_codes must not be empty")
        object.__setattr__(self, "reason_codes", reasons)
        if isinstance(self.case_count, bool) or not isinstance(self.case_count, int) or self.case_count < 0:
            raise ValueError("case_count must be a non-negative int")
        if self.evidence_level not in EVIDENCE_LEVELS:
            raise ValueError("unsupported evidence level")
        object.__setattr__(self, "partition", bounded_text(self.partition, "partition"))
        object.__setattr__(self, "candidate_policy_id", bounded_text(self.candidate_policy_id, "candidate_policy_id"))
        object.__setattr__(self, "baseline_policy_id", bounded_text(self.baseline_policy_id, "baseline_policy_id"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "passed": self.passed,
            "path": self.path,
            "reason_codes": list(self.reason_codes),
            "case_count": self.case_count,
            "evidence_level": self.evidence_level,
            "partition": self.partition,
            "candidate_policy_id": self.candidate_policy_id,
            "baseline_policy_id": self.baseline_policy_id,
        }


def evaluate_claim_gate(
    *,
    candidate_metrics: PolicyMetricsV1,
    baseline_metrics: PolicyMetricsV1,
    quality_difference_pp: BootstrapInterval,
    cost_reduction_pct: BootstrapInterval | None,
    latency_regression_pct: BootstrapInterval | None,
    case_count: int,
    evidence_level: str,
    partition: str,
    candidate_is_oracle: bool = False,
    blind_fields_exposed: bool = False,
    cost_complete: bool = True,
    latency_complete: bool = True,
    minimum_claim_cases: int = 30,
) -> ClaimGateResultV1:
    """Apply both accepted claim paths and return explicit failure reasons."""

    if not isinstance(candidate_metrics, PolicyMetricsV1) or not isinstance(baseline_metrics, PolicyMetricsV1):
        raise TypeError("candidate_metrics and baseline_metrics must be PolicyMetricsV1")
    if not isinstance(quality_difference_pp, BootstrapInterval):
        raise TypeError("quality_difference_pp must be BootstrapInterval")
    if cost_reduction_pct is not None and not isinstance(cost_reduction_pct, BootstrapInterval):
        raise TypeError("cost_reduction_pct must be BootstrapInterval or None")
    if latency_regression_pct is not None and not isinstance(latency_regression_pct, BootstrapInterval):
        raise TypeError("latency_regression_pct must be BootstrapInterval or None")
    if isinstance(case_count, bool) or not isinstance(case_count, int) or case_count < 0:
        raise ValueError("case_count must be a non-negative int")
    if isinstance(minimum_claim_cases, bool) or not isinstance(minimum_claim_cases, int) or minimum_claim_cases < 1:
        raise ValueError("minimum_claim_cases must be positive")
    if evidence_level not in EVIDENCE_LEVELS:
        raise ValueError("unsupported evidence level")
    candidate_id = candidate_metrics.policy_id
    baseline_id = baseline_metrics.policy_id
    reasons: list[str] = []
    if candidate_id == baseline_id:
        reasons.append("candidate_equals_baseline")
    if partition != "test":
        reasons.append("claims_require_test_partition")
    if EVIDENCE_LEVELS.index(evidence_level) < EVIDENCE_LEVELS.index("E1-public-replay"):
        reasons.append("evidence_below_e1")
    if candidate_is_oracle:
        reasons.append("candidate_oracle_disallowed")
    if blind_fields_exposed:
        reasons.append("blind_fields_exposed")
    if not cost_complete or cost_reduction_pct is None:
        reasons.append("cost_incomplete")
    if not latency_complete or latency_regression_pct is None:
        reasons.append("latency_incomplete")
    if case_count < minimum_claim_cases:
        reasons.append("insufficient_sample")
    if reasons:
        status = "insufficient_sample" if "insufficient_sample" in reasons else "not_supported"
        return ClaimGateResultV1(
            status=status,
            passed=False,
            path=None,
            reason_codes=tuple(reasons),
            case_count=case_count,
            evidence_level=evidence_level,
            partition=partition,
            candidate_policy_id=candidate_id,
            baseline_policy_id=baseline_id,
        )
    assert cost_reduction_pct is not None
    assert latency_regression_pct is not None
    latency_ok = latency_regression_pct.upper <= MAXIMUM_P95_LATENCY_REGRESSION_PCT
    path_non_inferior = (
        quality_difference_pp.lower >= QUALITY_NON_INFERIORITY_PP
        and cost_reduction_pct.lower >= MINIMUM_COST_REDUCTION_PCT
        and latency_ok
    )
    path_quality_gain = (
        cost_reduction_pct.lower >= 0.0
        and quality_difference_pp.lower >= MINIMUM_QUALITY_IMPROVEMENT_PP
        and latency_ok
    )
    if path_non_inferior or path_quality_gain:
        path = "cost_reduction_non_inferior_quality" if path_non_inferior else "quality_improvement_without_cost_regression"
        return ClaimGateResultV1(
            status="claimable",
            passed=True,
            path=path,
            reason_codes=("guardrails_passed", "statistical_path_passed"),
            case_count=case_count,
            evidence_level=evidence_level,
            partition=partition,
            candidate_policy_id=candidate_id,
            baseline_policy_id=baseline_id,
        )
    failed = ["statistical_paths_not_met"]
    if quality_difference_pp.lower < QUALITY_NON_INFERIORITY_PP:
        failed.append("quality_non_inferiority_failed")
    if cost_reduction_pct.lower < MINIMUM_COST_REDUCTION_PCT:
        failed.append("cost_reduction_threshold_failed")
    if latency_regression_pct.upper > MAXIMUM_P95_LATENCY_REGRESSION_PCT:
        failed.append("latency_regression_failed")
    return ClaimGateResultV1(
        status="not_supported",
        passed=False,
        path=None,
        reason_codes=tuple(failed),
        case_count=case_count,
        evidence_level=evidence_level,
        partition=partition,
        candidate_policy_id=candidate_id,
        baseline_policy_id=baseline_id,
    )


__all__ = [
    "ClaimGateResultV1",
    "MAXIMUM_P95_LATENCY_REGRESSION_PCT",
    "MINIMUM_COST_REDUCTION_PCT",
    "MINIMUM_QUALITY_IMPROVEMENT_PP",
    "QUALITY_NON_INFERIORITY_PP",
    "evaluate_claim_gate",
]
